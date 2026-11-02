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

VERSION = "0.1.0"
HELP = f"""session-inspect {VERSION}

Usage:
  python session_inspect.py overview [session-review options]
  python session_inspect.py find --scan <conversations.json> --kw <term> [--kw <term> ...]
  python session_inspect.py read [session-extract options]
  python session_inspect.py merge [session-extract --merge options]
  python session_inspect.py handoff [session-extract options]
  python session_inspect.py convert [session-extract --as openai options]

Modes:
  overview  Local Codex / Claude Code radar
  find      DeepSeek export-package corpus search
  read      Inspect a selected session or source file
  merge     Merge DeepSeek export and share-link products
  handoff   Create a sourced extraction product
  convert   Convert an extraction product for ChatGPT/OpenAI

Use '<mode> --help' for overview options, or consult SKILL.md for extraction examples.
"""
MODE_HELP = {
    "find": "Use: session_inspect.py find --scan <conversations.json> --kw <term> [--kw <term> ...]",
    "read": "Use: session_inspect.py read [session-extract options]. See session-extract usage in SKILL.md.",
    "merge": "Use: session_inspect.py merge --merge <export.json> --merge-with <share.json> --out <result.json>.",
    "handoff": "Use: session_inspect.py handoff [session-extract options] and set an explicit output path when writing a file.",
    "convert": "Use: session_inspect.py convert <product.json> [--out <chatgpt.json>].",
}


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
            print(MODE_HELP[mode])
            return 0
        if "--scan" not in rest:
            print(MODE_HELP[mode], file=sys.stderr)
            return 2
        result = session_extract.main(rest)
        return result if isinstance(result, int) else 0
    if mode in ("read", "extract", "merge", "handoff", "convert"):
        if rest in (["-h"], ["--help"]):
            print(MODE_HELP["read" if mode == "extract" else mode])
            return 0
        if mode == "convert" and "--as" not in rest:
            rest = [*rest, "--as", "openai"]
        result = session_extract.main(rest)
        return result if isinstance(result, int) else 0

    print(f"Unknown mode: {mode}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())