#!/usr/bin/env python3
"""Unified front door for session overview, search, extraction, and handoff."""
from __future__ import annotations

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

import session_extract
import session_review
import to_openai

VERSION = "0.8.0"
HELP = f"""session-inspect {VERSION}

Usage:
  python session_inspect.py overview [options]
  python session_inspect.py find --scan <conversations.json> --kw <term> [--kw <term> ...]
  python session_inspect.py find --session <uuid> --literal <text> [--json]
  python session_inspect.py read [options]
  python session_inspect.py merge --merge <export.json> --merge-with <share.json> [options]
  python session_inspect.py read --platform chatgpt-share "https://chatgpt.com/s/t_..." --outdir <dir>
  python session_inspect.py handoff [options] --out <product.json>
  python session_inspect.py convert <product.json> [--out <chatgpt.json>]
  python session_inspect.py render <product.json> [--out <transcript.md>]
  python session_inspect.py prune <product.json> --keep main,0,2 [--out <tree.json>]
  python session_inspect.py verify-package --package <dir> --product <product.json>

Modes:
  overview  Local Codex / Claude Code / T3Code radar
  find      DeepSeek export-package corpus search
  read      Inspect a selected session or source file
  merge     Merge DeepSeek export and share-link products
  handoff   Create a sourced extraction product
  convert   Convert an extraction product for ChatGPT/OpenAI
  render    Render a DeepSeek chat tree to branch-aware Markdown
  prune     Keep or drop branches of a DeepSeek chat tree
  verify-package  Package-level checks over a delivery directory
  chatgpt-* Read ChatGPT share links or signed-in conversations via a browser

Run '<mode> --help' for mode-specific options.
"""
MODE_HELP = {
    "find": "Usage: session_inspect.py find --scan <conversations.json> --kw <term> [--kw <term> ...] [--top N] [--json]\n       session_inspect.py find --session <uuid> --literal <text> [--case-sensitive] [--json]",
    "read": "Usage: session_inspect.py read [source] [options]\n       session_inspect.py read --platform chatgpt-share <share-url> --outdir <dir>",
    "merge": "Usage: session_inspect.py merge --merge <export.json> --merge-with <share.json> [--out <result.json> | --outdir <dir>] [--markdown]",
    "handoff": "Usage: session_inspect.py handoff [source] --out <product.json> [--as rescued|openai] [--verify]",
    "convert": "Usage: session_inspect.py convert <product.json> [--out <chatgpt.json>] [--slim] [--strict] [--stats]",
    "render": "Usage: session_inspect.py render <product.json> [--out <transcript.md>] [--current <node_id>] [--report]",
    "prune": "Usage: session_inspect.py prune <product.json> --keep all|main|<idx|node_id>[,...] [--out <tree.json>]",
    "verify-package": "Usage: session_inspect.py verify-package --package <dir> --product <product.json> [--transcript <md>] [--mainline <json>] [--index <md>] [--brief <md>] [--json]",
}

EXTRACT_OPTIONS_HELP = """Source options:
  --session <id>                 Locate a local session or exact T3 thread reference
  --summary <file>               Print only a work summary
  --platform codex|claude|t3|deepseek|deepseek-share|deepseek-private|chatgpt|chatgpt-share
  --ids <id>[,<id>]              DeepSeek export conversation IDs/prefixes
  --all                          Export every DeepSeek conversation
  --scan <conversations.json>    DeepSeek corpus search (use with --kw)
  --kw <term>                    Corpus keyword; repeatable
  --literal <text>               Exact literal search over native Codex sources
  --case-sensitive               Make --literal case-sensitive
  --cursor <token>               Resume a budgeted read page
  --budget <chars>               Character budget for one read page (default 6000)
  --top <N>                      Corpus search result limit
  --home <dir>                   Override local agent data home
  --t3-db <file>                 Override the read-only T3 statev2.sqlite path
  --proxy <url>                  CDP proxy for ChatGPT capture (default http://localhost:3456)
  --browser-context <id>         Pin ChatGPT capture to one browser context; never retries another context

Output options:
  --out <file>                   Single output file
  --outdir <dir>                 One product per conversation
  --as rescued|openai            Product shape (default: rescued)
  --title <name>                 Real title for DeepSeek share links
  --reason <text>                Rescue reason recorded in metadata
  --markdown                     Also write a Markdown preview
  --verify                       Verification receipt for the declared scope
  --json                         Machine-readable stdout; diagnostics go to stderr

Merge options:
  --merge <export.json>          DeepSeek export-package product
  --merge-with <share.json>      DeepSeek share-link product

Native codex/claude `handoff` requires --out and writes atomically.
`read --out` saves the same product it prints. T3/DeepSeek keep their own writers."""


def _extract_main(rest, mode="read"):
    result = session_extract.main(rest, entry_mode=mode)
    return result if isinstance(result, int) else 0


def _print_extract_help(mode):
    print(MODE_HELP[mode])
    if mode != "find":
        print()
        print(EXTRACT_OPTIONS_HELP)
    else:
        print("Repeats --kw; ranking favors title matches and rare terms.")


def _load_product(path):
    """读一个 rescued-session/v1 产物，返回 (doc, conv)。"""
    import json
    with open(path, "rb") as f:
        raw = f.read()
    doc = None
    for enc in ("utf-8-sig", "utf-16", "utf-8", "gbk"):
        try:
            doc = json.loads(raw.decode(enc))
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            doc = None
    if doc is None:
        raise SystemExit("ERROR: 不是可解析的 JSON：%s" % path)
    if isinstance(doc, dict) and "conversations" in doc:
        conv = (doc.get("conversations") or [{}])[0]
    else:
        conv = doc
    if not isinstance(conv, dict):
        raise SystemExit("ERROR: 不认识这个产物的形状：%s" % path)
    return doc, conv


def _tree_of(conv):
    """取出产物的聊天树；没有就如实报错（不要静默返回空树）。"""
    mapping = conv.get("raw_mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise SystemExit(
            "ERROR: 该产物没有 raw_mapping（聊天树），无法做树操作。\n"
            "  render/prune 只适用于 DeepSeek 容器产物；"
            "其他平台请用 read/convert。")
    return mapping


def _current_of(conv):
    """产物里主链末端节点（= 生成时的 current_message_id）。"""
    ids = [str(e.get("node_id")) for e in (conv.get("transcript") or [])
           if e.get("on_main_path")]
    return ids[-1] if ids else None


def _run_render(rest):
    import argparse
    import deepseek_tree
    ap = argparse.ArgumentParser(
        prog="session_inspect.py render",
        description="把 DeepSeek 聊天树渲染成分支感知的 Markdown（含全覆盖断言）")
    ap.add_argument("product", help="rescued-session/v1 产物 JSON")
    ap.add_argument("--out", default=None, help="输出 Markdown（省略写 stdout）")
    ap.add_argument("--current", default=None,
                    help="主链末端节点 id（省略则取产物里 on_main_path 的最后一个）")
    ap.add_argument("--title", default=None, help="覆盖标题")
    ap.add_argument("--report", action="store_true",
                    help="只打印结构体检报告（主链/支线/覆盖率），不渲染")
    a = ap.parse_args(rest)

    _doc, conv = _load_product(a.product)
    mapping = _tree_of(conv)
    current = a.current or _current_of(conv)

    if a.report:
        rep = deepseek_tree.render_report(mapping, current)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["complete"] else 3

    md = deepseek_tree.render_markdown(
        mapping, current, title=a.title or conv.get("title"))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md)
        sys.stderr.write("wrote %s\n" % a.out)
    else:
        sys.stdout.write(md)
    return 0


def _run_prune(rest):
    import argparse
    import deepseek_tree
    ap = argparse.ArgumentParser(
        prog="session_inspect.py prune",
        description="裁剪 / 保留 DeepSeek 聊天树的分支")
    ap.add_argument("product", help="rescued-session/v1 产物 JSON")
    ap.add_argument("--keep", required=True,
                    help="all | main | 分支序号或起点节点 id，逗号分隔；"
                         "如 'main'、'main,0,2'、'main,43'")
    ap.add_argument("--out", default=None, help="输出产物 JSON（省略写 stdout）")
    ap.add_argument("--current", default=None, help="主链末端节点 id")
    ap.add_argument("--report", action="store_true", help="只打印会保留/丢弃什么")
    a = ap.parse_args(rest)

    doc, conv = _load_product(a.product)
    mapping = _tree_of(conv)
    current = a.current or _current_of(conv)

    spec = a.keep.strip()
    if spec in ("all", "main"):
        sel = spec
    else:
        # ⚠️ 这里**不要**把数字串提前转成 int：`select_branches` 的规则是
        # **节点 id 优先**（DeepSeek 的 id 本身就是数字串，"5" 多半指 n5）。
        # 以前先在这转 int，等于把「id 优先」偷偷改成「序号优先」——
        # 两个模块口径不一致，同一份 --keep 在 CLI 和库里得到不同结果。
        # 一律原样传字符串，让 select_branches 自己做歧义消解。
        sel = [p.strip() for p in spec.split(",") if p.strip()]

    kept, report = deepseek_tree.select_branches(mapping, sel, current)
    if not [n for n in kept if n != "root"]:
        raise SystemExit("ERROR: --keep 没有保留任何消息节点（选择器解析失败？）")

    if a.report:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # 用裁剪后的树重建产物：transcript 只保留仍在树里的节点
    keep_ids = set(kept.keys())
    new_conv = dict(conv)
    new_conv["raw_mapping"] = kept
    new_conv["transcript"] = [
        e for e in (conv.get("transcript") or [])
        if str(e.get("node_id")) in keep_ids
    ]
    for i, e in enumerate(new_conv["transcript"]):
        e["seq"] = i
    # ⚠️ stats 必须按裁剪后的内容**重算**，不能只改 fragment_count：
    # 只改一个字段会留下一组自相矛盾的数字（节点少了、消息数还是旧的），
    # 下游按 stats 做核对时会得出「产物没丢」的错误结论。
    new_conv["stats"] = _rebuild_stats(conv, new_conv["transcript"], kept, current)
    new_conv["pruned"] = {
        "keep_spec": a.keep,
        "kept_node_count": report["kept_node_count"],
        "total_node_count": report["total_node_count"],
        "dropped_branch_indexes": report["dropped_branch_indexes"],
        "note": "裁剪不改动源产物；raw_mapping 已成为裁剪后的树。",
    }
    out_doc = dict(doc) if isinstance(doc, dict) and "conversations" in doc \
        else {"_meta": {}, "conversations": [new_conv]}
    out_doc["conversations"] = [new_conv]

    text = json.dumps(out_doc, ensure_ascii=False, indent=1)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        sys.stderr.write("wrote %s（保留 %d/%d 节点）\n"
                         % (a.out, report["kept_node_count"],
                            report["total_node_count"]))
    else:
        sys.stdout.write(text)
    return 0


def _rebuild_stats(conv, transcript, kept, current):
    """
    裁剪后按**实际保留的节点与条目**重算 stats。

    口径（与 deepseek_private 的 adapt 对齐）：
      - fragment_count / message_count 从 transcript 重数
      - 主链消息数 = 裁剪后仍能沿 current 回溯到的消息节点数
      - 其余为离路径消息数
    ⚠️ 旧 stats 里认不出的自定义字段一律**原样保留**——本 skill 不想
    因为裁剪就把别的采集器写进去的元信息抹掉。
    """
    import deepseek_tree
    stats = dict(conv.get("stats") or {})
    main = deepseek_tree.main_path_ids(kept, current)
    node_ids = [str(e.get("node_id")) for e in transcript]
    stats["fragment_count"] = len(transcript)
    # 消息节点数：按去重后的 node_id 算；「消息」= 有 REQUEST/RESPONSE 的节点
    msg_nodes = []
    for nid in node_ids:
        if nid not in msg_nodes:
            msg_nodes.append(nid)
    stats["message_count"] = len(msg_nodes)
    stats["main_path_message_count"] = len([n for n in main if n != "root"])
    stats["off_path_message_count"] = len([n for n in msg_nodes if n not in set(main)])
    stats["node_count"] = len([n for n in kept if str(n) != "root"])
    return stats


def _branch_starts(mapping, current):
    """当前树里各支线的起点节点 id 集合（用于 --keep 的歧义消解）。"""
    import deepseek_tree
    return {str(nodes[0]) for _f, nodes in
            deepseek_tree.branch_index(mapping, None, current)}


def _run_verify_package(rest):
    import verify_package
    return verify_package.main(rest)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(HELP)
        return 0
    if args[0] == "--version":
        print(f"session-inspect {VERSION}")
        return 0

    mode, rest = args[0], args[1:]
    if mode in ("overview", "scan"):
        return session_review.main(rest)
    if mode == "find":
        if rest in (["-h"], ["--help"]):
            _print_extract_help(mode)
            return 0
        if "--scan" not in rest and "--literal" not in rest:
            print(MODE_HELP[mode], file=sys.stderr)
            return 2
        return _extract_main(rest)
    if mode in ("read", "extract", "merge", "handoff"):
        if rest in (["-h"], ["--help"]):
            _print_extract_help("read" if mode == "extract" else mode)
            return 0
        return _extract_main(rest, mode=mode)
    if mode == "convert":
        if rest in (["-h"], ["--help"]):
            print(MODE_HELP[mode])
            print()
            print("Converts an existing rescued-session/v1 product; use handoff --as openai for a new extraction.")
            return 0
        return to_openai.main(rest)
    if mode == "render":
        return _run_render(rest)
    if mode == "prune":
        return _run_prune(rest)
    if mode in ("verify-package", "verify_package"):
        return _run_verify_package(rest)

    print(f"Unknown mode: {mode}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
