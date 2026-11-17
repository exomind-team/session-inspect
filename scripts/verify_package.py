# -*- coding: utf-8 -*-
"""
**包级**校验器：独立复核一个「交接包 / 提取包」的每条自称，不复用生成时的中间变量。

为什么需要它（与既有校验器的分工）：
    - `extract_deepseek.verify_payload` 之类是**单产物自证**：
      证明「这一份 JSON 没丢没重」。
    - `verify_merged_deepseek.py` 是**并集专用**：证明「并集没丢 snippet」。
    - **包级校验是第三种**：证明「这个交付目录里，文档说的和文件里的对得上」——
      路径都存在、全录覆盖了树上每个节点、正文没被截断、单线严格交替、
      附件对账、引用的节点号真实存在、没有占位符残留。
    这一层以前每次都是**临时手写**，而它恰恰是抓出「全录少 18% 节点」那次
    事故的唯一手段。所以固化成可复用脚本。

设计铁律（沿用 desensitize_scan 的教训）：
    1. **不许假绿**：校验本身没跑成时退出码 2，绝不报「通过」。
    2. 断言要**独立**：不复用生成脚本的中间变量，重新从文件读。
    3. 路径**不写死**：全部由参数传入，样本换了用参数覆盖。
    4. Windows 上 stdout 编码跟随控制台码页（复用 deepseek_common）。

退出码：0 = 全部通过；1 = 有断言失败；2 = 校验本身没跑成（**不是「通过」**）。

用法：
    python verify_package.py --package <交付目录> \
        --product <产物.json> [--transcript <全录.md>] \
        [--mainline <主线单线.json>] [--index <附件索引.md>] \
        [--brief <简报.md>] [--json]
"""
import argparse
import json
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from deepseek_common import setup_streams
except ImportError:  # pragma: no cover
    def setup_streams():
        return "utf-8"


def _read_text(path):
    """读文本（显式关闭句柄，避免 ResourceWarning）。"""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Report:
    """收集断言结果。"""

    def __init__(self):
        self.passes = []
        self.fails = []
        self.warns = []

    def ok(self, cond, msg, warn=False):
        if cond:
            self.passes.append(msg)
        else:
            (self.warns if warn else self.fails).append(msg)
        return bool(cond)

    def note(self, msg):
        self.warns.append(msg)


# ============================================================
# 各组断言
# ============================================================

def check_paths(rep, package, expect):
    """[1] 包内声明的路径全部存在。"""
    for rel in expect:
        rep.ok(os.path.exists(os.path.join(package, rel)), "存在 " + rel)


def check_requested_docs(rep, docs):
    """
    [0] **显式请求的文档必须存在**——不存在是 FAIL，不是跳过。

    ⚠️ 这条是「不许假绿」的核心：如果调用方给了 `--transcript` 但路径写错，
    后面所有依赖全录的检查都会「跳过」，最后报 PASS 0 FAIL——
    **看起来一切正常，其实最关键的那条根本没跑**。
    所以「请求了但没找到」必须判失败；只有「压根没请求」才算跳过。
    """
    for path in docs:
        rep.ok(os.path.exists(path), "请求的文档存在：%s" % path)


def check_transcript_coverage(rep, product, transcript_path):
    """
    [2] 全录的消息节点 == 树上的全部节点（且无重复）。

    ⚠️ 这条是抓出「手写节点清单只覆盖 94/122」那次事故的断言。
    """
    if not transcript_path or not os.path.exists(transcript_path):
        rep.note("未提供全录，跳过节点覆盖检查")
        return
    md = _read_text(transcript_path)
    # ⚠️ 节点 id 用 `\S+` 而不是 `\d+`：`render_markdown` 原样输出 id，
    # 而 `deepseek_tree` 并不约束 id 必须是数字。用 `\d+` 会把一棵
    # 完全正常的非数字 id 树判成「0 节点覆盖」，诊断还会反过来说用户的
    # 文件格式不对——错怪用户比沉默更糟。
    heads = re.findall(r"^\*\*▸ n(\S+) · ", md, re.M)
    # ⚠️ 只在真的解析出节点时才断言「无重复」：空全录里 0 条天然「无重复」，
    # 那行 PASS 纯属噪声，还会被人当成「这条检查过了」。
    if heads:
        rep.ok(len(heads) == len(set(heads)),
               "全录消息节点无重复（%d 条）" % len(heads))
    conv = (product.get("conversations") or [{}])[0]
    mapping = conv.get("raw_mapping") or {}
    all_nodes = {str(n) for n in mapping if str(n) != "root"}
    if not all_nodes:
        # 两种可能：产物真没有树，或树只有 root 哨兵没有任何消息节点。
        # 两种情况都**核对不了**覆盖——但「核对不了」不等于「通过」，
        # 而且这里不能只记一句 WARN 就溜走（那正是假绿的形状）。
        rep.ok(False, "产物没有可核对的树节点（raw_mapping 缺失或为空），"
                      "无法证明全录覆盖完整——不判定为通过")
        return
    shown = set(heads)
    rep.ok(shown <= all_nodes, "全录节点全部存在于树中")
    rep.ok(shown == all_nodes,
           "全录覆盖全部 %d 个节点（实到 %d，缺 %s）"
           % (len(all_nodes), len(shown), sorted(all_nodes - shown)[:10]))


def check_no_truncation(rep, product, transcript_path):
    """
    [3] 全录正文无截断：逐条消息做**首尾锚点**子串检查。

    只比总字符数会放过「中间被切」——所以取正文首尾各 60 字当锚点。
    """
    if not transcript_path or not os.path.exists(transcript_path):
        rep.note("未提供全录，跳过截断检查")
        return
    md = _read_text(transcript_path)
    conv = (product.get("conversations") or [{}])[0]
    src = [(str(e.get("node_id")), e.get("content") or "")
           for e in conv.get("transcript") or []
           if e.get("type") in ("REQUEST", "RESPONSE")]

    # ⚠️ 分块正则必须容得下**渲染器实际产出的格式**：
    #     `**▸ n1 · 👤 用户 · 2026-01-01 10:30**\n正文`
    # 标题后**紧跟正文**，没有空行。早先这里要求 `\*\*\n\n`，
    # 结果 `re.split` 一块都切不出来，by_node_md 永远空——
    # 每条消息都被判「内容不在全录里」，检查退化成「恒 FAIL」或
    # （换个写法）「恒 PASS」。这里改成匹配行尾、不吞正文。
    blocks = re.split(r"^\*\*▸ n(\S+) · [^\n]*\*\*[ \t]*\r?\n", md, flags=re.M)
    by_node_md = {}
    for i in range(1, len(blocks) - 1, 2):
        by_node_md.setdefault(blocks[i], []).append(blocks[i + 1])

    # 如果一条 node 都没切出来，说明全录不是本渲染器的格式——
    # 这时**既不能**默默把所有消息判成截断（假红），**也不能**判成通过（假绿）。
    # 调用方既然显式给了 --transcript，就说明它认为这份文件应当可核对；
    # 核对不了必须判 FAIL，否则「路径/格式写错」会伪装成「全部通过」。
    if not by_node_md:
        rep.ok(False,
               "全录里没有 `**▸ n<id> · …**` 形式的节点标记，无法核对截断"
               "（该文件可能不是 render 的产出，或路径指错了）")
        return

    truncated = []
    missing_nodes = []
    for nid, content in src:
        if not content:
            continue
        if nid not in by_node_md:
            missing_nodes.append(nid)
            continue
        joined = "\n".join(by_node_md[nid])
        if content[:60] not in joined or content[-60:] not in joined:
            truncated.append(nid)
    rep.ok(not missing_nodes,
           "产物里的消息节点都能在全录里找到（缺 %d 个：%s）"
           % (len(missing_nodes), missing_nodes[:8]))
    rep.ok(not truncated,
           "%d 条正文首尾锚点全在（截断 %d 条：%s）"
           % (len([c for _n, c in src if c]), len(truncated), truncated[:8]))


def check_mainline(rep, product, mainline_path):
    """[4] 主线单线：角色严格交替，且每条与产物逐字一致。"""
    if not mainline_path or not os.path.exists(mainline_path):
        rep.note("未提供主线单线，跳过交替检查")
        return
    ml = _read_json(mainline_path)
    ms = ml.get("messages") or []
    # ⚠️ 0 条消息不能算「严格交替」通过：空列表天然满足「没有相邻同角色」，
    # 于是这条断言会在**根本没读到单线**时亮绿灯。宁可判失败。
    if not ms:
        rep.ok(False, "主线单线是空的（0 条消息）——"
                      "通常是把 prune 的产物或别的 JSON 当成单线了；"
                      "单线应由 convert 产出")
        return
    bad = [i for i in range(1, len(ms)) if ms[i].get("role") == ms[i - 1].get("role")]
    rep.ok(not bad, "主线单线角色严格交替（%d 条消息）" % len(ms))

    conv = (product.get("conversations") or [{}])[0]
    main_ids = []
    for e in conv.get("transcript") or []:
        if e.get("on_main_path") and str(e.get("node_id")) not in main_ids:
            main_ids.append(str(e.get("node_id")))
    if not main_ids:
        rep.note("产物没有 on_main_path 标记，跳过逐字比对")
        return
    req, resp = {}, {}
    for e in conv.get("transcript") or []:
        if not e.get("on_main_path"):
            continue
        nid = str(e.get("node_id"))
        if e.get("type") == "REQUEST":
            req[nid] = e.get("content") or ""
        elif e.get("type") == "RESPONSE":
            resp[nid] = e.get("content") or ""
    exp = []
    for nid in main_ids:
        if nid in req:
            exp.append(("user", req[nid]))
        if nid in resp:
            exp.append(("assistant", resp[nid]))
    rep.ok(len(exp) == len(ms), "主线单线条数一致（%d vs %d）" % (len(exp), len(ms)))
    diff = sum(1 for i in range(min(len(exp), len(ms)))
               if exp[i][0] != ms[i].get("role") or exp[i][1] != ms[i].get("content"))
    rep.ok(diff == 0, "主线单线每条 role/content 与产物逐字一致（差异 %d 条）" % diff)


def check_attachments(rep, package, product, index_path):
    """
    [5] 附件对账：索引里声明落地的路径都存在；产物里的附件数与索引一致。
    """
    conv = (product.get("conversations") or [{}])[0]
    atts = conv.get("attachments") or []
    landed = [a for a in atts if a.get("local_path")]
    rep.ok(True, "产物附件 %d 条（其中已落地字节 %d 条）" % (len(atts), len(landed)))

    if not index_path or not os.path.exists(index_path):
        rep.note("未提供附件索引，跳过索引对账")
        return
    idx = open(index_path, encoding="utf-8").read()
    paths = re.findall(r"\| `((?:附件|attachments)/[^`]+)` \|", idx)
    missing = [p for p in paths
               if not os.path.exists(os.path.join(package, p.replace("/", os.sep)))]
    rep.ok(not missing, "索引声明的 %d 个落地路径都存在（缺 %s）"
           % (len(paths), missing[:5]))


def check_cited_nodes(rep, product, docs):
    """[6] 文档里引用的节点号真实存在于树中。"""
    conv = (product.get("conversations") or [{}])[0]
    mapping = conv.get("raw_mapping") or {}
    all_nodes = {str(n) for n in mapping if str(n) != "root"}
    if not all_nodes:
        rep.note("产物没有 raw_mapping，跳过引用节点检查")
        return
    for path in docs:
        if not path or not os.path.exists(path):
            continue
        text = _read_text(path)
        # ⚠️ 识别「节点引用」用的是**结构性标记**，不是任意 `n` + 字母：
        # 正文和 URL 里到处都是 "n" 开头的英文单词，放宽成正则会制造大量假红
        # （实测：新闻链接 `newsDetail_forward_31748710` 被当成节点引用）。
        # 真正的引用只有两种稳定形态：
        #   (a) 渲染器的消息标记  `**▸ n<id> ·`
        #   (b) 行内代码/反引号里的 `n<id>`（简报里的写法）
        # 节点 id 也据此放宽到非数字，与 render 的口径一致。
        cited = set(re.findall(r"▸ n(\S+?) ·", text))
        cited |= set(re.findall(r"`n(\S+?)`", text))
        missing = sorted(cited - all_nodes,
                         key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
        rep.ok(not missing,
               "%s 引用的节点号全部存在（缺 %s）"
               % (os.path.basename(path), missing[:8]))


def check_placeholders(rep, docs):
    """[7] 无占位符 / 未完成标记残留。"""
    pats = ["@@", "TODO", "FIXME", "{{PASSWORD_", "待补"]
    for path in docs:
        if not path or not os.path.exists(path):
            continue
        text = _read_text(path)
        for pat in pats:
            if pat in text:
                rep.ok(False, "%s 含残留 %r" % (os.path.basename(path), pat))
    rep.ok(True, "无占位符残留")


def check_self_verification(rep, product):
    """[8] 产物自带的自证字段是 PASS。"""
    v = (product.get("_meta") or {}).get("verification") or {}
    if not v:
        rep.note("产物没有 _meta.verification 自证字段")
        return
    rep.ok(v.get("ok") is True, "产物自证 ok=True")
    rep.ok(v.get("missing_count", 0) == 0, "产物自证 missing_count=0")
    rep.ok(v.get("duplicate_count", 0) == 0, "产物自证 duplicate_count=0")


# ============================================================
# 入口
# ============================================================

def verify(package, product_path, transcript=None, mainline=None,
           index=None, brief=None, extra_docs=None, json_out=False):
    rep = Report()

    # ⚠️ 校验**本身没跑成** → 退出码 2，与「跑了但没通过」（1）区分开。
    # 报 0 或 1 都会让调用方以为「验过了」。
    if not os.path.isdir(package):
        sys.stderr.write("ERROR: --package 不是目录：%s\n" % package)
        return 2
    if not os.path.exists(product_path):
        sys.stderr.write("ERROR: --product 不存在：%s\n" % product_path)
        return 2
    with open(product_path, "rb") as f:
        raw = f.read()
    product = None
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            product = json.loads(raw.decode(enc))
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            product = None
    if product is None:
        sys.stderr.write("ERROR: --product 不是可解析的 JSON：%s\n" % product_path)
        return 2

    docs = [p for p in (transcript, mainline, index, brief) if p]
    docs += list(extra_docs or [])

    check_requested_docs(rep, docs)
    check_paths(rep, package, [
        os.path.relpath(p, package).replace(os.sep, "/") for p in docs
        if os.path.exists(p)])
    check_transcript_coverage(rep, product, transcript)
    check_no_truncation(rep, product, transcript)
    check_mainline(rep, product, mainline)
    check_attachments(rep, package, product, index)
    check_cited_nodes(rep, product, docs)
    check_placeholders(rep, docs)
    check_self_verification(rep, product)

    if json_out:
        print(json.dumps({
            "pass": len(rep.passes), "fail": len(rep.fails), "warn": len(rep.warns),
            "fails": rep.fails, "warns": rep.warns,
        }, ensure_ascii=False, indent=2))
    else:
        for m in rep.passes:
            print("  PASS  " + m)
        for m in rep.warns:
            print("  WARN  " + m)
        for m in rep.fails:
            print("  FAIL  " + m)
        print("=" * 56)
        print("PASS %d  WARN %d  FAIL %d" % (len(rep.passes), len(rep.warns), len(rep.fails)))
    return 1 if rep.fails else 0


def main(argv=None):
    setup_streams()
    ap = argparse.ArgumentParser(description="交接包 / 提取包的包级校验器")
    ap.add_argument("--package", required=True, help="交付目录")
    ap.add_argument("--product", required=True, help="产物 JSON（rescued-session/v1）")
    ap.add_argument("--transcript", default=None, help="全录 Markdown")
    ap.add_argument("--mainline", default=None, help="主线单线 JSON")
    ap.add_argument("--index", default=None, help="附件索引 Markdown")
    ap.add_argument("--brief", default=None, help="简报 Markdown")
    ap.add_argument("--doc", action="append", default=[], help="额外要检查的文档（可重复）")
    ap.add_argument("--json", action="store_true", dest="json_out")
    a = ap.parse_args(argv)
    return verify(a.package, a.product, transcript=a.transcript,
                  mainline=a.mainline, index=a.index, brief=a.brief,
                  extra_docs=a.doc, json_out=a.json_out)


if __name__ == "__main__":
    raise SystemExit(main())
