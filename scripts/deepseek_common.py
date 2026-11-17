# -*- coding: utf-8 -*-
"""
DeepSeek 两条路径（账号导出包 / 分享链接）共用的归一规则。

为什么要有这个模块：
1. **标题占位符**：分享 API 返回的标题是占位符，但**字符串会变**
   （实测出现过「来自分享的对话」与「Shared Conversation」两种）。
   硬编码 `== "来自分享的对话"` 会让 `title_is_placeholder` 静默误判为 False——
   不报错，只是给出一个假的「一切正常」信号。改成名单 + 归一比较。
2. **URL 引用锚点**：DeepSeek 的搜索结果 URL 带引用锚点
   （如 `.../paper.pdf#7#22#7`），**同一篇文档在不同引用位置锚点不同**。
   按原始 URL 建字典 = 同一篇文章被登记成多条记录，且**互相不补字段**。
   实测（2026-10-29 样本：分享侧 494 条结果）239 个唯一原始 URL、只有 238 个唯一文档；
   按原始 URL 去重会吞掉 63,309 个字符（52.8%）的 snippet。
   统一在这里归一，提取侧与合并侧用**同一把尺子**。

规则只在这里定义一次，别的模块 import 过来用——
「同一事实两处实现」是本 skill 历史上已经踩过的坑（merge 的 url 键
与 file_id 归一化不同步）。
"""
import json
import os
import re
import sys


def console_encoding():
    """
    stdout 该用什么编码。

    ⚠️ **不要在 Windows 上钉死 UTF-8**：PowerShell 5.1 读取子进程 stdout 时
    按 `[Console]::OutputEncoding`（控制台码页，中文默认 cp936）**解码**。
    子进程写 UTF-8 字节 → 被按 GBK 解码 → **中文全乱码，而且不报错**
    （静默损坏比崩溃更危险）。

    所以跟随**控制台真实码页**：
    - Windows：用 `GetConsoleOutputCP()` 拿真实码页（chcp 65001 → UTF-8）
    - 其他：UTF-8

    需要**字节级保真**的产物请用 `--out` / `--outdir`——
    文件侧显式 `encoding="utf-8"`，不受控制台码页影响。
    """
    if os.name != "nt":
        return "utf-8"
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        if cp == 65001:
            return "utf-8"
        if cp:
            # ⚠️ 顺序要紧：必须**先试码页本身**。
            # 以前把 "utf-8" 放在第一位，而它必然可编码 → 函数恒返回 utf-8，
            # 算出来的 cp 从未被使用 → 「跟随码页」其实是**假的**，
            # 在标准中文 Windows 上照样静默乱码。
            for enc in ("cp" + str(cp), "gbk"):
                try:
                    "x".encode(enc)
                    return enc
                except LookupError:
                    continue
    except Exception:
        pass
    return "utf-8"


def setup_streams():
    """把 stdout/stderr 的编码设成控制台码页（错误字符用 replace 兜底）。"""
    enc = console_encoding()
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except (AttributeError, ValueError):
            pass
    return enc

# 分享链接侧的占位符标题。DeepSeek 前端可能改文案/语言，所以用名单 + 归一比较，
# 而不是「等于某一个字符串」。
PLACEHOLDER_TITLES = (
    "来自分享的对话",
    "Shared Conversation",
    "Shared conversation",
    "Shared Conversation ",
)

_WS = re.compile(r"\s+")


def normalize_title(title):
    """标题归一：去空白 + 折叠内部空白 + 去掉首尾不可见字符。"""
    if not isinstance(title, str):
        return ""
    return _WS.sub(" ", title.replace("​", "").replace("﻿", "")).strip()


def is_placeholder_title(title):
    """标题是不是「分享链接占位符」——用于判断 title_is_placeholder。"""
    t = normalize_title(title)
    if not t:
        return False
    return any(normalize_title(p) == t for p in PLACEHOLDER_TITLES)


def norm_url(url):
    """
    URL 归一（主键用）：剥掉 `#` 之后的引用锚点。

    `https://x/a.pdf#7#22#7` → `https://x/a.pdf`
    非字符串原样返回；`#` 在开头（纯锚点）时返回原值。
    """
    if not isinstance(url, str):
        return url
    base, sep, _frag = url.partition("#")
    return base if base else url


def url_anchor(url):
    """取引用锚点（不含 `#`）。没有则返回 None。"""
    if not isinstance(url, str):
        return None
    _base, sep, frag = url.partition("#")
    return frag if (sep and frag) else None


# 结果载荷里**派生**出来的字段——它们是归一产物，不是源数据。
# 计算 fragment 签名时要剥掉，否则加了派生字段会让签名整体漂移，
# 进而把「源 vs 产物」的比较结果变成噪声。
DERIVED_RESULT_KEYS = ("url_normalized", "url_anchor")


def strip_derived_result_fields(results):
    """剥掉结果里的派生字段，返回新的 list（不改原对象）。"""
    if not results:
        return results
    out = []
    for r in results:
        if not isinstance(r, dict):
            out.append(r)
            continue
        out.append({k: v for k, v in r.items() if k not in DERIVED_RESULT_KEYS})
    return out


def result_url_key(result):
    """
    结果条目的合并主键：**归一后的 URL**。

    优先用已算好的 `url_normalized`，否则现算——这样导出包路径
    （没有派生字段）和分享链接路径（带派生字段）能用同一把尺子。
    """
    if not isinstance(result, dict):
        return None
    u = result.get("url_normalized") or result.get("url")
    return norm_url(u)


# ============================================================
# 跨源比对：归一签名
# ============================================================
#
# 为什么需要这一层：
# 跨源比对（导出包 vs 分享链接、私密会话 vs 分享快照）时，
# **直接对 fragment 求签名多重集差集会产生假阳性**。
# 实测（2026-11 样本）：分享侧多出 21 条「独有」fragment，
# 逐条查下来**全是派生字段与签名路径造成的签名漂移**，不是内容差异：
#   - 派生字段：`url_normalized` / `url_anchor`（归一产物，非源数据）
#   - 签名路径：附件的 `signed_path` 每次请求都变（短时签名）
# 判「内容是否一致」必须**先剥掉这些易变字段再比**，否则会得出
# 「两个源内容不同」的错误结论，进而白做一轮人工逐条核对。

# 结果条目里**每次采集都会变**的字段（短时签名路径等）。
# 与 DERIVED_RESULT_KEYS 分开：那些是**归一产物**，这些是**易变载荷**。
#
# ⚠️ `url_original` **不属于这一组**（曾误放在这里，已挪出）：
# 它是「该文档首次出现时的原始 URL」，是**出处/内容**，同一源两次采集稳定不变，
# 且是唯一记录「这段结果来自哪个引用」的字段（见 merge_deepseek.py）。
# 把它当易变字段剥掉，会让两份**不同文档**的比对判成相等——
# 那正是本函数要消掉的反面（假阴性）。它该和 `url` 一样走 norm_url。
VOLATILE_RESULT_KEYS = ("signed_path", "download_url")

# 附件条目里易变的字段（签名路径、下载回执）。
VOLATILE_FILE_KEYS = ("signed_path", "download_url", "local_path", "sha256",
                      "bytes", "content_type", "variants")


def _strip_keys(obj, keys):
    """递归剥掉 dict / list 里的指定键，返回新对象（不改原对象）。"""
    if isinstance(obj, dict):
        return {k: _strip_keys(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [_strip_keys(v, keys) for v in obj]
    return obj


def _normalise_result_urls(obj):
    """
    把结果里的 `url` 归一到主键形态（剥引用锚点）。

    ⚠️ 只剥派生字段**不够**：引用锚点也可能直接写在 `url` 里
    （导出包侧 `.../a.pdf#7#22#7`，分享侧 `.../a.pdf`）。
    跨源比对时两条本应相等，不归一就会误报「内容不同」。
    """
    if isinstance(obj, dict):
        out = {k: _normalise_result_urls(v) for k, v in obj.items()}
        # 两处 URL 都**归一**而不是**丢弃**：
        # `url` 是检索主键，`url_original` 是出处（含首次出现的引用锚点）。
        # 归一能把「同一文档不同锚点」收敛掉，但只要换成另一篇文档就仍不相等。
        for key in ("url", "url_original"):
            if isinstance(out.get(key), str):
                out[key] = norm_url(out[key])
        return out
    if isinstance(obj, list):
        return [_normalise_result_urls(v) for v in obj]
    return obj


def normalise_for_compare(results=None, files=None):
    """
    把比对用的 results / files 归一：剥派生字段、剥易变载荷、归一 URL。

    返回 (results', files')，都是新对象。

    ⚠️ **空容器与缺省必须归一成同一个东西**：不同提取器对「没有结果」
    的表达不一样（一个写 `results: []`，另一个干脆不写这个键）。若不归一，
    签名会只因 `[]` vs 缺失而不同，跨源比对就会报出**莫须有的差异**——
    这正是本函数要消掉的那类假阳性。
    """
    out_r = None
    if results is not None:
        out_r = _normalise_result_urls(
            _strip_keys(strip_derived_result_fields(results), VOLATILE_RESULT_KEYS))
        if out_r in (None, [], {}):
            out_r = None
    out_f = _strip_keys(files, VOLATILE_FILE_KEYS) if files is not None else None
    if out_f in (None, [], {}):
        out_f = None
    return out_r, out_f


def frag_signature_normalized(frag):
    """
    **归一后的** fragment 签名——跨源比对专用。

    与 extract_deepseek.frag_signature 的区别：
      - 先剥掉派生字段（url_normalized / url_anchor）
      - 再剥掉易变载荷（signed_path / download_url 等）
    这样「同一内容、两次采集」的签名才相等，差集才只反映**真实差异**。

    ⚠️ 单源自证（产物 vs 自己的源）**不要**用这个——那种场合要的是
    逐字节保真，任何差异都该报出来。这个签名只用于**跨源**判断
    「两个源讲的是不是同一件事」。
    """
    if not isinstance(frag, dict):
        return (None, "", "", "")
    results, files = normalise_for_compare(frag.get("results"), frag.get("files"))
    return (
        frag.get("type"),
        frag.get("content") or "",
        json.dumps(results, ensure_ascii=False, sort_keys=True) if results is not None else "",
        json.dumps(files, ensure_ascii=False, sort_keys=True) if files is not None else "",
    )
