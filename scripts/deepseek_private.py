# -*- coding: utf-8 -*-
"""
DeepSeek **登录态私密会话**适配器（第三种容器）。

为什么需要这个模块：
    DeepSeek 有三种容器，正文 schema 一致但**外层结构各不相同**：

    | 容器 | 入口 | 外层结构 | skill 里的处理 |
    |---|---|---|---|
    | 账号导出包 | `conversations.json` | `mapping` 聊天树 | `extract_deepseek.py` |
    | 公开分享链接 | `/share/<id>` | 线性 `messages[]` | `extract_deepseek_share.py` |
    | **登录态私密会话** | `/a/chat/s/<uuid>` | `chat_messages[]` + `parent_id` 树 | **本模块** |

    第三种最容易被误判成分享链接——把 `/a/chat/s/<uuid>` 交给分享提取器会得到
    `not a share id or share url`；再拿它的 uuid 去问 share API 会得到
    `share does not exist`。这两条报错都指向「这不是分享链接」，
    但它**也不是**导出包，所以以前只能手搓一个适配器。

    本模块把那次的适配器固化成**一等路径**，且**不重新实现解析**：
    `iter_fragments` / `frag_signature` / `build_stats` / `FRAGMENT_ROLES`
    全部从 `extract_deepseek` 复用，树操作复用 `deepseek_tree`，
    保证三条路径的口径**不可能漂移**。

抓取（为什么必须在浏览器里做）：
    外部 `curl` 直接请求 `history_messages` 会被 WAF 拦下，返回一个
    `Error - Request Blocked` 的 HTML 页——**HTTP 200 的 HTML**，不是 4xx，
    很容易被误判成「接口坏了」。必须在**已登录页面内** fetch，带着正常的
    浏览器指纹与同源 Cookie 才通。取回的响应有几十万字符，
    一次性拿会被 CDP 截断，所以走 `browser_proxy.evaluate_chunked` 分块取回。

⚠️ 凭据边界：
    `localStorage.userToken` 存的是 **JSON 对象**，真正的 token 在 `.value`
    （64 字符）。取错了会得到 `40003 INVALID_TOKEN`——**看起来像「会话不存在」**。
    token 只在内存里用于 Authorization 头，**不落盘、不进日志、不进产物**。
"""
import datetime
import json
import os
import sys
from collections import Counter

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from extract_deepseek import (  # noqa: E402
    FRAGMENT_ROLES,
    build_stats,
    frag_signature,
    iter_fragments,
)
from deepseek_tree import main_path_ids as _tree_main_path_ids  # noqa: E402
from deepseek_tree import num_key as _num_key  # noqa: E402

SCHEMA = "rescued-session/v1"
PLATFORM = "deepseek-web-private"

# 私密会话的 API 端点模板。
HISTORY_ENDPOINT = "/api/v0/chat/history_messages?chat_session_id={sid}"
API_ORIGIN = "https://chat.deepseek.com"

# 登录态 token 在 localStorage 里的键与取值的字段名。
TOKEN_KEY = "userToken"
TOKEN_VALUE_FIELD = "value"

# 私密会话的 signed_path 对**匿名 GET 恒 400**（实测 p/t/d/f 四种 ty 全 400）。
# 要字节必须走登录态下载控件。这里如实记「未取字节」，不假装拿到。
ATTACHMENT_BYTES_NEED_LOGIN = True


class PrivateSessionError(RuntimeError):
    """私密会话抓取/适配失败。"""


def _iso(ts):
    """epoch 秒 → ISO8601（+08:00）。None 原样返回。"""
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    return datetime.datetime.fromtimestamp(
        float(ts), datetime.timezone(datetime.timedelta(hours=8))
    ).isoformat()


def _now_iso():
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).isoformat()


# ============================================================
# 容器识别
# ============================================================

def is_private_session_url(url):
    """`/a/chat/s/<uuid>` 是登录态私密会话；`/share/<id>` 是分享链接。"""
    if not isinstance(url, str):
        return False
    u = url.strip()
    if "/share/" in u:
        return False
    return "/a/chat/s/" in u


def session_id_from_url(url):
    """从 `/a/chat/s/<uuid>` 抠出 uuid；抠不到返回 None。"""
    if not isinstance(url, str):
        return None
    marker = "/a/chat/s/"
    i = url.find(marker)
    if i < 0:
        return None
    rest = url[i + len(marker):]
    for stop in ("?", "#", "/"):
        j = rest.find(stop)
        if j >= 0:
            rest = rest[:j]
    return rest.strip() or None


def looks_like_biz_data(obj):
    """判断一个 dict 是不是 history_messages 的 biz_data。"""
    return isinstance(obj, dict) and "chat_messages" in obj and "chat_session" in obj


def unwrap_biz_data(doc):
    """
    从原始响应里取出 biz_data。

    容忍几种外壳：`{data:{biz_data:{...}}}`（实测）、`{biz_data:{...}}`、
    直接就是 biz_data。取不到就抛错——**不要静默返回空**。
    """
    if looks_like_biz_data(doc):
        return doc
    if isinstance(doc, dict):
        data = doc.get("data")
        if isinstance(data, dict):
            biz = data.get("biz_data")
            if looks_like_biz_data(biz):
                return biz
            if looks_like_biz_data(data):
                return data
        biz = doc.get("biz_data")
        if looks_like_biz_data(biz):
            return biz
    raise PrivateSessionError(
        "响应里没有 chat_messages/chat_session——不是 history_messages 的 biz_data。"
        "（若响应是 HTML，多半是被 WAF 拦了：请确认是在已登录页面内 fetch。）")


# ============================================================
# 树构建
# ============================================================

def dangling_parent_ids(msgs):
    """
    返回**指向未返回消息**的 `parent_id` 集合（已排序）。

    这类节点在原始树里是断链的：它既不是顶层节点（有 parent），
    父又不存在（从 root 走不到）。`build_mapping` 会把它们上移成顶层，
    但**真实性必须记下来**——否则产物会自称「全部验证通过」。
    """
    known = {str(m.get("message_id")) for m in msgs} | {"root", "None"}
    return sorted({str(m.get("parent_id")) for m in msgs
                   if m.get("parent_id") is not None
                   and str(m.get("parent_id")) not in known}, key=_num_key)


def build_mapping(msgs):
    """
    `chat_messages[]` → `{node_id: {id,parent,children,message}}`（含 root 哨兵）。

    与账号导出包的 mapping 形状对齐，这样 `iter_fragments` / `deepseek_tree`
    等共用函数无需感知容器差异。
    """
    kids = {}
    for m in msgs:
        parent = m.get("parent_id")
        kids.setdefault(str(parent) if parent is not None else "None", []).append(
            str(m["message_id"]))
    # ⚠️ `parent_id` 指向一条 **API 没返回**的消息时，按上面的分桶它既不是
    # root 的孩子、也不是任何人的孩子——节点会「看起来是顶层节点」而孤立。
    # 这种树 `deepseek_tree.coverage` 会判为不可达，但**适配器自己的多重集
    # 自证看不见**（两侧都源自本函数产出的 mapping，同源不独立）。
    # 所以这里直接把这些 id 找出来交给 verification 如实报出。
    dangling = dangling_parent_ids(msgs)
    root_children = sorted(kids.get("None", []), key=_num_key)
    # 父悬空的节点当作**顶层**挂到 root 下：与其让它孤立（从 root 走不到，
    # 渲染时静默消失），不如让它可达、同时把断链如实记进 verification。
    for pid in dangling:
        for nid in kids.get(pid, []):
            if nid not in root_children:
                root_children.append(nid)
    root_children = sorted(root_children, key=_num_key)
    mapping = {"root": {"id": "root", "parent": None,
                        "children": root_children, "message": None}}
    for m in msgs:
        nid = str(m["message_id"])
        parent = m.get("parent_id")
        parent_s = str(parent) if parent is not None else "root"
        if parent_s in dangling:
            parent_s = "root"      # 断链上移，见上
        mapping[nid] = {
            "id": nid,
            # ⚠️ 顶层消息的 `parent_id` 是 None，但它在 root.children 里。
            # 必须把 parent 记成 `"root"` 而不是 None——否则 mapping 内部
            # `parent`/`children` 不一致（节点在 root.children 里、parent 却说
            # 没有父），覆盖率一致性检查会把它判成「不可达」。
            # 账号导出包路径本来就是这么记的，这里对齐。
            "parent": parent_s,
            "children": sorted(kids.get(nid, []), key=_num_key),
            "message": m,
        }
    return mapping


def _num_key(x):
    """数字字符串按数值排，非数字按字符串排（DeepSeek 的 node id 是数字）。

    实现已下沉到 `deepseek_tree.num_key`，这里只保留旧名做兼容——
    两处各写一份迟早漂移。
    """
    from deepseek_tree import num_key
    return num_key(x)


def main_path_ids(msgs, current_id):
    """主链：从 current_message_id 沿 parent 回溯（复用 deepseek_tree 的口径）。"""
    mapping = build_mapping(msgs)
    return _tree_main_path_ids(mapping, str(current_id) if current_id is not None else None)


# ============================================================
# 适配
# ============================================================

def _entry(seq, mid, depth, msg, frag, on_main):
    """fragment → transcript 条目（与另两条路径同形状）。"""
    ftype = frag.get("type")
    role, label = FRAGMENT_ROLES.get(ftype, ("unknown", ftype))
    e = {
        "seq": seq,
        "node_id": str(mid),
        "depth": depth,
        "type": ftype,
        "role": role,
        "label": label,
        "model": msg.get("model"),
        "timestamp": _iso(msg.get("inserted_at")),
        "sources": ["private-api"],
        "on_main_path": on_main,
    }
    for key in ("content", "results", "result", "files", "references", "queries"):
        if frag.get(key) is not None:
            e[key] = frag[key]
    return e


def _collect_attachments(msgs):
    """附件清单。私密会话的字节需登录态下载控件，这里如实记「未取字节」。"""
    items = []
    seen = set()
    for m in msgs:
        question = ""
        for f in m.get("fragments") or []:
            if f.get("type") == "REQUEST":
                question = str(f.get("content", ""))
                break
        for f in m.get("fragments") or []:
            if f.get("type") != "FILE":
                continue
            for x in f.get("files") or []:
                key = (x.get("id"), m["message_id"])
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "file_id": x.get("id"),
                    "file_name": x.get("file_name"),
                    "file_size_bytes": x.get("file_size"),
                    "node_id": str(m["message_id"]),
                    "timestamp": _iso(m.get("inserted_at")),
                    "question_it_answers": question,
                    "width": x.get("width"),
                    "height": x.get("height"),
                    "is_image": x.get("is_image"),
                    "bytes_available_in_export": False,
                    "bytes_available": False,
                    "local_path": None,
                    "signed_path_present": bool(x.get("signed_path")),
                    "bytes_note": (
                        "私密会话的 signed_path 对匿名 GET 恒 400（实测 p/t/d/f 四种 ty 全 400）；"
                        "要字节必须走登录态下载控件。"
                    ) if ATTACHMENT_BYTES_NEED_LOGIN else None,
                })
    items.sort(key=lambda x: x.get("timestamp") or "")
    return items


def adapt(biz, reason=None, session_id=None):
    """
    biz_data → rescued-session/v1 产物。

    分叉处理：**不**像账号导出包那样 DFS 全树（那会把兄弟分支拼成
    自相矛盾的对话）。沿 `current_message_id` 回溯出主链，其余节点按
    `inserted_at` 补捞并标 `on_main_path: false` + `fork_of`——
    既不丢数据，也不假装它们是同一条脉络。
    """
    cs = biz.get("chat_session") or {}
    msgs = biz.get("chat_messages") or []
    if not msgs:
        raise PrivateSessionError("chat_messages 为空——没有可适配的消息。")

    mapping = build_mapping(msgs)
    dangling = dangling_parent_ids(msgs)
    main = main_path_ids(msgs, cs.get("current_message_id"))
    main_set = set(main)

    by_id = {str(m["message_id"]): m for m in msgs}
    transcript = []
    seq = 0
    for depth, mid in enumerate(main):
        msg = by_id.get(str(mid))
        if not msg:
            continue
        for frag in msg.get("fragments") or []:
            transcript.append(_entry(seq, mid, depth, msg, frag, True))
            seq += 1

    off = [m for m in msgs if str(m["message_id"]) not in main_set]
    for msg in sorted(off, key=lambda m: m.get("inserted_at") or 0):
        for frag in msg.get("fragments") or []:
            e = _entry(seq, msg["message_id"], -1, msg, frag, False)
            e["fork_of"] = (str(msg.get("parent_id"))
                            if msg.get("parent_id") is not None else None)
            transcript.append(e)
            seq += 1

    conv = {"id": cs.get("id") or session_id, "title": cs.get("title"),
            "inserted_at": _iso(cs.get("inserted_at")),
            "updated_at": _iso(cs.get("updated_at")),
            "mapping": mapping}
    stats = build_stats(conv, transcript)
    stats["main_path_message_count"] = len(main)
    stats["off_path_message_count"] = len(off)

    # 自证：产物 transcript vs 源全部消息的 fragment 多重集
    src = Counter()
    for _nid, _msg, frag in iter_fragments(conv):
        src[frag_signature(frag)] += 1
    out = Counter()
    for e in transcript:
        out[frag_signature(e)] += 1
    missing, extra = src - out, out - src

    payload = {
        "id": conv["id"],
        "title": conv["title"],
        "title_source": "private-api",
        "title_is_placeholder": False,
        "inserted_at": conv["inserted_at"],
        "updated_at": conv["updated_at"],
        "rescue_reason": reason,
        "stats": stats,
        "attachments": _collect_attachments(msgs),
        "transcript": transcript,
        # 原样保留整棵树：transcript 只是给人读的视图
        "raw_mapping": mapping,
        "raw_biz_data_keys": sorted(biz.keys()),
    }

    meta = {
        "schema": SCHEMA,
        "platform": PLATFORM,
        "source": {
            "kind": "private-session-api",
            "session_id": conv["id"],
            "endpoint": ("GET %s%s" % (API_ORIGIN,
                                       HISTORY_ENDPOINT.format(sid=conv["id"]))),
            "auth": "登录态 Bearer（token 取自页面 localStorage，未落盘）",
            "message_count_in_source": len(msgs),
            "fragment_count_in_source": sum(src.values()),
            "captured_at": _now_iso(),
            "note": ("容器为 chat_messages[] + parent_id 树，与账号导出包的 mapping、"
                     "分享链接的线性数组均不同；正文 schema 三者一致。"),
        },
        "extraction": {
            "tool": "deepseek_private.py（容器适配）+ extract_deepseek 口径复用",
            "mode": "adapt",
            "requested_ids": [conv["id"]] if conv["id"] else [],
            "rescue_reason": reason,
            "note": ("主链沿 current_message_id 回溯；非主链节点标 on_main_path=false "
                     "并按 inserted_at 补捞，raw_mapping 保留整棵树。"),
        },
        "conversation_count": 1,
        "verification": {
            # ⚠️ 多重集自证**看不到断链**：两侧都源自本函数的 mapping，
            # 同源不独立。所以显式把断链纳入判定（`dangling` 见上）。
            "ok": not missing and not extra and not dangling,
            "source_fragment_count": sum(src.values()),
            "output_fragment_count": len(transcript),
            "missing_count": sum(missing.values()),
            "duplicate_count": sum(extra.values()),
            "seq_contiguous": [e["seq"] for e in transcript] == list(range(len(transcript))),
            "raw_mapping_identical": payload["raw_mapping"] == mapping,
            "main_path_message_count": len(main),
            "off_path_message_count": len(off),
            # 父指向「API 未返回的消息」的 id——已上挂到 root 保住可达性，
            # 但树本身是断的，不许自称完整。
            "dangling_parent_count": len(dangling),
            "dangling_parent_ids": dangling[:20],
        },
    }
    return {"_meta": meta, "conversations": [payload]}


# ============================================================
# 抓取（CDP，已登录页面内 fetch + 分块取回）
# ============================================================

def fetch_private(session_id, *, proxy=None, target=None, timeout=180,
                  settle_timeout=300, keep_tab=False, on_progress=None,
                  browser_context_id=None):
    """
    抓取一份登录态私密会话的原始 biz_data。

    session_id —— 会话 uuid
    target     —— 复用一个已登录的标签页（推荐）。不传则新开一个 DeepSeek 页面。
    browser_context_id —— 新开标签页时用哪个浏览器上下文（登录态 profile）。
        ⚠️ 忽略它的后果很隐蔽：会落到默认上下文，若那个 profile 没登录，
        报错会是「localStorage 里没有 userToken」，**误导成「你没登录」**。
    keep_tab   —— True 时不关掉自己开的标签页（调试用）。

    返回 (biz_data, target_id)。

    ⚠️ 这里的每一步都是被真实踩过的坑：
      - 必须在**已登录页面内** fetch（外部 curl 被 WAF 拦成 200 的 HTML）
      - token 取自 `localStorage.userToken` 的 `.value`（不是裸字符串）
      - 响应几十万字符，必须**分块取回**，否则被 CDP 静默截断
    """
    import browser_proxy as bp

    proxy = proxy or bp.DEFAULT_PROXY
    if not bp.proxy_ready(proxy):
        raise PrivateSessionError(
            "浏览器代理不可达（%s）。请先启动 web-access 代理；"
            "代理不可达 ≠ 会话不存在。" % proxy)

    opened = False
    if not target:
        if browser_context_id:
            # 指定了上下文就**必须**在那个上下文里开页；否则登录态可能不在。
            import chatgpt_web
            target = chatgpt_web.open_in_context(
                browser_context_id, API_ORIGIN + "/", proxy=proxy)
        else:
            target = bp.open_tab(API_ORIGIN + "/", proxy=proxy)
        opened = True
    try:
        token = bp.read_local_storage(target, TOKEN_KEY, proxy=proxy,
                                      value_field=TOKEN_VALUE_FIELD)
        if not token:
            raise PrivateSessionError(
                "页面 localStorage 里没有 %s——多半是**未登录**。"
                "请先在浏览器里登录 DeepSeek 再重试。" % TOKEN_KEY)

        url = API_ORIGIN + HISTORY_ENDPOINT.format(sid=session_id)
        if on_progress:
            on_progress("fetching %s" % url)
        text = bp.in_page_fetch_json(
            target, url, proxy=proxy,
            headers={"Authorization": "Bearer " + token},
            timeout=timeout, settle_timeout=settle_timeout)
        bp.clear_chunked(target, proxy=proxy)

        if not text or text.lstrip()[:1] == "<":
            raise PrivateSessionError(
                "响应不是 JSON（多半是 WAF 拦截页或登录态失效）。"
                "前 200 字符：%r" % (text or "")[:200])

        doc = json.loads(text)
        # 业务错误码：40003 INVALID_TOKEN 最容易被误判成「会话不存在」
        if isinstance(doc, dict):
            code = doc.get("code")
            if code not in (None, 0):
                msg = doc.get("msg") or doc.get("message") or ""
                hint = ""
                if str(code) == "40003" or "TOKEN" in str(msg).upper():
                    hint = ("（40003/INVALID_TOKEN 通常不是「会话不存在」，"
                            "而是 token 取错了或登录态过期）")
                raise PrivateSessionError(
                    "接口返回业务错误 code=%s msg=%s%s" % (code, msg, hint))
        return unwrap_biz_data(doc), target
    finally:
        if opened and not keep_tab:
            bp.close_tab(target, proxy=proxy)
