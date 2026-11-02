# session-inspect

Unified entry point for exploring and inspecting Codex, Claude Code, and DeepSeek conversation histories.

The Agent-facing contract is in `SKILL.md`. Invoke the bundled dispatcher at `scripts/session_inspect.py`; it routes overview work to the former review modules and targeted extraction/DeepSeek workflows to the existing extraction modules.

This repository intentionally uses no persistent index or database. Original session files are read-only; exports should be written to an explicit destination outside this repository.