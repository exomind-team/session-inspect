# session-inspect

Unified entry point for T3Code thread entry, native Codex and Claude Code sessions, and DeepSeek conversation histories.

Public repository: <https://github.com/exomind-team/session-inspect>

This repository supersedes two retired, archived skills:

- [session-review](https://github.com/ARCJ137442/session-review) - use `session_inspect.py overview`.
- [session-extract](https://github.com/ARCJ137442/session-extract-skill) - use `session_inspect.py read`, `find`, `merge`, `handoff`, or `convert`.

The Agent-facing contract is in `SKILL.md`. Invoke the bundled dispatcher at `scripts/session_inspect.py`; it routes overview work to the review modules and targeted extraction/DeepSeek workflows to the extraction modules. The former scripts are bundled as internal modules, not separate entry points.

This repository creates no persistent index or database of its own. Original sessions and the existing T3 state database are read-only; exports should be written to an explicit destination outside this repository.

## Codex core

`scripts/codex_core.py` is the single implementation of Codex source identity, JSONL framing, message-body extraction, mirror relations, ordering metadata and coverage statistics. The radar (`scan_codex.py`), the detailed reader (`extract_codex.py`) and the T3 native enrichment all resolve Codex sessions through this core, so their identity and body semantics cannot drift. Do not add a second Codex parser; extend the core instead.

## Install

Clone this repository into a user-level skills directory:

```powershell
git clone https://github.com/exomind-team/session-inspect.git "$HOME\.claude\skills\session-inspect"
```

Requires Python 3.9+ and uses only the standard library.

## Quick start

```powershell
python scripts/session_inspect.py overview --days 7
python scripts/session_inspect.py overview --session <uuid> --json
python scripts/session_inspect.py find --scan <conversations.json> --kw <topic>
python scripts/session_inspect.py read --session <uuid>
python scripts/session_inspect.py read --platform t3 --session "t3-context://v1/thread/thread_<uuid>"
python scripts/session_inspect.py read --platform t3 --session "t3-context://v1/thread/thread_<uuid>" --out <out.json>
python scripts/session_inspect.py read --platform deepseek-share <share-url> --outdir <dir> --verify
python scripts/session_inspect.py convert <product.json> --out <chatgpt.json>
python scripts/session_inspect.py handoff --session <uuid> --out <product.json> --verify
python scripts/session_inspect.py handoff --session <uuid> --out <product.json> --as openai
```

Run `python scripts/session_inspect.py <mode> --help` for mode-specific options.
