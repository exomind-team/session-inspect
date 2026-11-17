# -*- coding: utf-8 -*-
"""
web-access CDP 代理的**共享原语层**。

为什么要有这个模块：
    浏览器抓取（ChatGPT 网页版、DeepSeek 登录态会话……）都跑在同一个
    CDP 代理上，用的都是同一套 `/new` `/eval` `/close` `/targets` 端点。
    以前这套原语只长在 `chatgpt_web.py` 里，别的抓取路径要用就得
    **复制一份**——两处实现迟早漂移（本 skill 历史上已经踩过
    「同一事实两处实现」的坑，见 deepseek_common 的注释）。

本模块提供两类东西：
  1. **代理原语**（从 chatgpt_web 抽出，行为不变）：
     _request / proxy_ready / list_contexts / open_tab / close_tab /
     navigate / evaluate
  2. **大载荷与登录态两个通用 helper**（原本每次现搓）：
     - evaluate_chunked —— 分块取回超大返回值
     - read_local_storage —— 从页面 localStorage 读登录态 token

⚠️ 安全边界（本模块**只读**，且**不落盘凭据**）：
    - 只读页面，绝不发送消息或修改会话；
    - `read_local_storage` 把 token 交给调用方**在内存里**用于 Authorization 头，
      **不写文件、不进日志、不进产物**。调用方有责任不把它落盘。
    - 代理不可达时抛 BrowserError，调用方应如实报「代理不可达」，
      而不是伪造成「会话不存在」。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


DEFAULT_PROXY = "http://localhost:3456"


class BrowserError(RuntimeError):
    """浏览器面无法驱动，或没有产出可用结果。"""

    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# ============================================================
# 代理原语
# ============================================================

def _request(path: str, *, proxy: str, data: Optional[bytes] = None,
             timeout: int = 60) -> bytes:
    url = proxy.rstrip("/") + path
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise BrowserError(
            "BROWSER_PROXY_ERROR",
            "browser proxy returned HTTP {}{}".format(
                exc.code, ": " + detail[:1200] if detail else ""),
            retryable=exc.code >= 500,
        ) from exc
    except TimeoutError as exc:
        endpoint = path.partition("?")[0]
        raise BrowserError(
            "BROWSER_TIMEOUT",
            f"browser proxy request timed out after {timeout}s at {endpoint}",
            retryable=True,
        ) from exc
    except urllib.error.URLError as exc:
        raise BrowserError(
            "BROWSER_UNAVAILABLE",
            "cannot reach the browser proxy at {}: {}".format(proxy, exc),
            retryable=True,
        ) from exc


def proxy_ready(proxy: str = DEFAULT_PROXY) -> bool:
    try:
        _request("/targets", proxy=proxy, timeout=5)
        return True
    except BrowserError:
        return False


def list_contexts(proxy: str = DEFAULT_PROXY) -> Dict[str, list]:
    """按浏览器上下文分组列出已打开的标签页。"""
    raw = _request("/targets", proxy=proxy, timeout=20)
    targets = json.loads(raw.decode("utf-8-sig"))
    contexts: Dict[str, list] = {}
    for target in targets:
        if target.get("type") != "page":
            continue
        context = target.get("browserContextId") or ""
        contexts.setdefault(context, []).append(target)
    return contexts


def open_tab(url: str, *, proxy: str = DEFAULT_PROXY) -> str:
    query = urllib.parse.urlencode({"url": url})
    payload = json.loads(_request("/new?" + query, proxy=proxy, timeout=90))
    target = payload.get("targetId") or ""
    if not target:
        raise BrowserError("TAB_CREATE_FAILED", "proxy did not return a target id")
    return target


def close_tab(target: str, *, proxy: str = DEFAULT_PROXY) -> None:
    try:
        _request("/close?target=" + urllib.parse.quote(target),
                 proxy=proxy, timeout=20)
    except BrowserError:
        pass


def navigate(target: str, url: str, *, proxy: str = DEFAULT_PROXY,
             timeout_s: int = 60) -> None:
    query = urllib.parse.urlencode({"target": target, "url": url})
    _request("/navigate?" + query, proxy=proxy, timeout=timeout_s)


def evaluate(target: str, script: str, *, proxy: str = DEFAULT_PROXY,
             timeout: int = 90) -> Any:
    """在目标标签页里求值一段 JS，返回解析后的值。

    代理会把返回值 JSON 序列化；字符串若本身是 JSON 会被再解析一层。
    脚本可以用 `(async () => { ... })()` 形式（代理会 await 它）。
    """
    raw = _request("/eval?target=" + urllib.parse.quote(target),
                   proxy=proxy, data=script.encode("utf-8"), timeout=timeout)
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if "error" in payload:
        raise BrowserError("EVAL_FAILED", str(payload["error"]),
                           retryable="timeout" in str(payload["error"]).lower())
    value = payload.get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ============================================================
# 大载荷：分块取回
# ============================================================

# 每块字符数。CDP 的返回值经代理转一手，**一次性取回几十万字符会被截断**
# （实测 632,411 字符的响应无法一次拿全）。切成小块最稳。
DEFAULT_CHUNK_CHARS = 40000


def evaluate_chunked(target: str, script: str, *, proxy: str = DEFAULT_PROXY,
                     chunk_chars: int = DEFAULT_CHUNK_CHARS,
                     timeout: int = 90, settle_timeout: int = 240) -> str:
    """
    求值一段**会产出超大字符串**的 JS，分块取回。

    做法：脚本把结果存进 `window.__chunked_result`（字符串），
    本函数再反复 `/eval` 按 `slice(offset, offset+chunk_chars)` 取回。
    这样每次响应都很小，不会被截断。

    script 的约定：**最后一句必须是 `window.__chunked_result = <字符串>`**，
    或者直接 `return` 一个字符串（此时本函数会把它当作已就绪的结果）。
    为避免竞态，脚本应把结果赋值给 `window.__chunked_result` 并返回其长度。

    返回：拼好的完整字符串。

    ⚠️ 为什么不用「一次取回」：这是**静默截断**——拿到的 JSON 短一截，
    `json.loads` 可能仍成功（如果截在合法位置），于是下游以为数据完整。
    分块是唯一能保证完整的手段。
    """
    # 1) 先跑脚本，让它把结果落到 window.__chunked_result
    probe = evaluate(target, script, proxy=proxy, timeout=settle_timeout)
    total = None
    # ⚠️ 必须排除 bool：Python 的 bool 是 int 的子类，`isinstance(True, int)` 为真。
    # 一个很自然的写法 `window.__chunked_result = big; true` 会返回 True，
    # 于是 total 被当成 1——但循环里 offset 从 0 起仍会切出第一块 40000 字符，
    # 之后 offset(40000) < total(1) 为假、循环就停了：**静默截断成一块**。
    # 这正是本函数写来要防的那种失败。用 `type(probe) is int` 精确判定。
    if (type(probe) is int or type(probe) is float) and not isinstance(probe, bool):
        total = int(probe)
    elif isinstance(probe, str):
        # 脚本直接返回了字符串：已经拿到全部，直接回
        return probe

    if total is None or total <= 0:
        # 探针没给长度（或给了不可能是长度的值）：**回查真实长度**，不要猜。
        got = evaluate(target, "String(window.__chunked_result || '').length",
                       proxy=proxy, timeout=timeout)
        if isinstance(got, str) and not got.strip():
            total = 0
        elif isinstance(got, bool) or got is None:
            total = 0
        else:
            try:
                total = int(got)
            except (TypeError, ValueError):
                raise BrowserError(
                    "CHUNKED_LENGTH_UNREADABLE",
                    "无法确定分块结果长度（读到 %r）；为免静默截断，这里报错。" % (got,))

    out = []
    offset = 0
    while offset < total:
        # ⚠️ 这里有两个必须一起解决的坑：
        #
        # 1) **码元 vs 码点**：JS 的 `slice` 按 **UTF-16 码元**计数，
        #    而 Python 的 `len` 按 **码点**计数。一个星形平面字符（emoji、
        #    部分罕见汉字）在 JS 里占 2 个码元、Python 里只算 1。
        #    用 `offset += len(part)` 推进会让 offset **偏小**，
        #    下一块从头重叠，拼出来**中间有重复且不报错**。
        #
        # 2) **代理对不能切断**：即使 offset 算对了，只要切点落在代理对中间，
        #    Python 侧拿到的就是**落单的 surogate**——而 Python 的字符串拼接
        #    **不会**把高代理和低代理重新组合成原字符。
        #    结果是一个「看起来长度差不多、实际内容已损坏」的字符串，
        #    之后按 UTF-8 写出还会炸。
        #
        # 所以：让 JS 报告实际推进的码元数，并且**切点若落在代理对中间就回退一格**。
        raw = evaluate(
            target,
            "(function(){var s=String(window.__chunked_result||'');"
            "var end=Math.min(%d, s.length);"
            "if (end < s.length) {"
            "  var c=s.charCodeAt(end-1);"
            "  if (c>=0xD800 && c<=0xDBFF) { end=end-1; }"
            "}"
            "var p=s.slice(%d, end);"
            "return JSON.stringify({t:p, n:end-%d});})()"
            % (offset + chunk_chars, offset, offset),
            proxy=proxy, timeout=timeout)
        if isinstance(raw, str):
            # ⚠️ 上面的 JS **总是**返回 JSON.stringify 的结果。所以解析失败
            # 只可能是「响应被截断/被改写」——正是本函数要防的那种损坏。
            # 以前这里 fallback 成 `{"t": raw, ...}`，等于**把损坏的内容
            # 当成正常分块吞下去**。宁可报错，不可静默入库。
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BrowserError(
                    "CHUNKED_CHUNK_UNPARSABLE",
                    "分块响应不是预期 JSON（偏移 %d）——疑似被截断或改写：%r"
                    % (offset, raw[:120])) from exc
        if not isinstance(raw, dict) or "t" not in raw:
            raise BrowserError(
                "CHUNKED_CHUNK_UNPARSABLE",
                "分块响应缺少 t 字段（偏移 %d）：%r" % (offset, str(raw)[:120]))
        part = (raw or {}).get("t")
        if not isinstance(part, str):
            part = "" if part is None else str(part)
        units = (raw or {}).get("n")
        if not isinstance(units, int) or units <= 0:
            units = len(part)
        if not part:
            # 取回空块但 total 说还有内容——如实报错，不要静默短一截
            raise BrowserError(
                "CHUNKED_FETCH_INCOMPLETE",
                "分块取回在第 %d 码元处返回空块（预期总长 %d）" % (offset, total))
        out.append(part)
        offset += units
    return "".join(out)


def clear_chunked(target: str, *, proxy: str = DEFAULT_PROXY) -> None:
    """清掉分块暂存，避免大字符串长期占着页面内存。"""
    try:
        evaluate(target, "window.__chunked_result = null; true", proxy=proxy, timeout=20)
    except BrowserError:
        pass


# ============================================================
# 登录态：从页面 localStorage 读 token
# ============================================================

def read_local_storage(target: str, key: str, *, proxy: str = DEFAULT_PROXY,
                       value_field: str = "value", timeout: int = 30) -> Optional[str]:
    """
    从目标页面的 localStorage 读一个键，返回其中的 token 字符串。

    为什么要单独做成 helper（一次真实踩坑）：
        DeepSeek 的 `localStorage.userToken` 存的是**一个 JSON 对象**
        （`{"value": "<64 字符 token>", ...}`），不是裸字符串。
        直接 `localStorage.getItem('userToken')` 拿到的是 JSON 文本，
        拿去当 Bearer token 会得到 `40003 INVALID_TOKEN`——
        **看起来像「会话不存在」，其实是 token 取错了**。

    本函数按 `value_field` 从 JSON 里取真正那一段；取不到就原样返回。

    ⚠️ 返回值是**凭据**：只在内存里用于 Authorization 头，
    **不要写文件、不要进日志、不要进产物**。
    """
    script = (
        "(function(){"
        "  var raw = localStorage.getItem(%s);"
        "  if (raw === null) return null;"
        "  try { var o = JSON.parse(raw);"
        "        if (o && typeof o === 'object' && o[%s] !== undefined)"
        "          return String(o[%s]);"
        "  } catch (e) {}"
        "  return String(raw);"
        "})()"
        % (json.dumps(key), json.dumps(value_field), json.dumps(value_field))
    )
    value = evaluate(target, script, proxy=proxy, timeout=timeout)
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def in_page_fetch_json(target: str, url: str, *, proxy: str = DEFAULT_PROXY,
                       headers: Optional[Dict[str, str]] = None,
                       method: str = "GET", body: Optional[str] = None,
                       timeout: int = 120, settle_timeout: int = 240) -> str:
    """
    在**已登录页面的上下文里** fetch 一个 API，把响应文本分块取回。

    为什么必须在页面里 fetch（一次真实踩坑）：
        外部 `curl` 直接请求 DeepSeek 的 history_messages 端点会被 WAF 拦下，
        返回一个 `Error - Request Blocked` 的 HTML 页——**不是 4xx，是 200 的 HTML**，
        很容易被误判成「接口坏了」。
        在已登录页面内 fetch 则带着正常的浏览器指纹与同源 Cookie，能通。

    ⚠️ headers 里若带 Authorization，其值**不落盘**；本函数只把响应文本带出。
    返回：响应文本（分块拼好的完整字符串）。
    """
    hdrs = dict(headers or {})
    script = (
        "(async () => {"
        "  const r = await fetch(%s, {method: %s, headers: %s%s});"
        "  const t = await r.text();"
        "  window.__chunked_result = t;"
        "  return t.length;"
        "})()"
        % (json.dumps(url), json.dumps(method), json.dumps(hdrs),
           (", body: " + json.dumps(body)) if body is not None else "")
    )
    return evaluate_chunked(target, script, proxy=proxy,
                            timeout=timeout, settle_timeout=settle_timeout)
