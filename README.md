# session-inspect

Unified entry point for exploring and inspecting Codex, Claude Code, and DeepSeek conversation histories.

Public repository: <https://github.com/exomind-team/session-inspect>

This repository supersedes two retired, archived skills:

- [session-review](https://github.com/ARCJ137442/session-review) - use `session_inspect.py overview`.
- [session-extract](https://github.com/ARCJ137442/session-extract-skill) - use `session_inspect.py read`, `find`, `merge`, `handoff`, or `convert`.

The Agent-facing contract is in `SKILL.md`. Invoke the bundled dispatcher at `scripts/session_inspect.py`; it routes overview work to the review modules and targeted extraction/DeepSeek workflows to the extraction modules. The former scripts are bundled as internal modules, not separate entry points.

This repository intentionally uses no persistent index or database. Original session files are read-only; exports should be written to an explicit destination outside this repository.

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
python scripts/session_inspect.py read --platform deepseek-share <share-url> --outdir <dir> --verify
python scripts/session_inspect.py convert <product.json> --out <chatgpt.json>
```

Run `python scripts/session_inspect.py <mode> --help` for mode-specific options.
