#!/usr/bin/env python3
"""Unified front door for session overview, search, extraction, and handoff."""
from __future__ import annotations

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

import session_extract
import session_review
import to_openai

VERSION = "0.4.0"
HELP = f"""session-inspect {VERSION}

Usage:
  python session_inspect.py overview [options]
  python session_inspect.py find --scan <conversations.json> --kw <term> [--kw <term> ...]
  python session_inspect.py read [options]
  python session_inspect.py merge --merge <export.json> --merge-with <share.json> [options]
  python session_inspect.py handoff [options] --out <product.json>
  python session_inspect.py convert <product.json> [--out <chatgpt.json>]

Modes:
  overview  Local Codex / Claude Code / T3Code radar
  find      DeepSeek export-package corpus search
  read      Inspect a selected session or source file
  merge     Merge DeepSeek export and share-link products
  handoff   Create a sourced extraction product
  convert   Convert an extraction product for ChatGPT/OpenAI

Run '<mode> --help' for mode-specific options.
"""
MODE_HELP = {
    "find": "Usage: session_inspect.py find --scan <conversations.json> --kw <term> [--kw <term> ...] [--top N] [--json]",
    "read": "Usage: session_inspect.py read [source] [options]",
    "merge": "Usage: session_inspect.py merge --merge <export.json> --merge-with <share.json> [--out <result.json> | --outdir <dir>] [--markdown]",
    "handoff": "Usage: session_inspect.py handoff [source] --out <product.json> [--as rescued|openai] [--verify]",
    "convert": "Usage: session_inspect.py convert <product.json> [--out <chatgpt.json>] [--slim] [--strict] [--stats]",
}

EXTRACT_OPTIONS_HELP = """Source options:
  --session <id>                 Locate a local session or exact T3 thread reference
  --summary <file>               Print only a work summary
  --platform codex|claude|t3|deepseek|deepseek-share
  --ids <id>[,<id>]              DeepSeek export conversation IDs/prefixes
  --all                          Export every DeepSeek conversation
  --scan <conversations.json>    DeepSeek corpus search (use with --kw)
  --kw <term>                    Corpus keyword; repeatable
  --top <N>                      Corpus search result limit
  --home <dir>                   Override local agent data home
  --t3-db <file>                 Override the read-only T3 statev2.sqlite path

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
        if "--scan" not in rest:
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

    print(f"Unknown mode: {mode}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
