# session-inspect

Unified entry point for T3Code thread entry, native Codex and Claude Code sessions, and DeepSeek conversation histories.

It also reads ChatGPT share links (`https://chatgpt.com/s/...`) and signed-in
conversations through a controllable browser tab. The share page renders its
content client-side, so a plain HTTP request returns a shell; the browser path
reads the rendered DOM and can download listed attachments into an explicit
output directory.

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
python scripts/session_inspect.py find --session <uuid> --literal "不做计费" --json
python scripts/session_inspect.py read --session <uuid> --budget 6000 --json
python scripts/session_inspect.py read --platform chatgpt-share "https://chatgpt.com/s/t_..." --outdir <dir> --verify
python scripts/session_inspect.py read --platform chatgpt "https://chatgpt.com/g/g-p-.../c/<uuid>" --outdir <dir>
python scripts/session_inspect.py read --platform chatgpt "https://chatgpt.com/g/g-p-.../c/<uuid>" --browser-context <id> --outdir <dir> --verify
```

## ChatGPT web capture

`chatgpt-share` and `chatgpt` require the `web-access` CDP proxy (default
`http://localhost:3456`, override with `--proxy`). The collector:

- reads the rendered transcript by turn and message id;
- lists attachments and images separately as listed / fetched;
- downloads attachment bytes only through the page's own download control
  into an explicit `--outdir`, with SHA-256 recorded, within a bounded
  20-second total attachment pass;
- writes the verified transcript product before attempting attachment downloads,
  so a slow or unavailable file does not discard readable messages;
- accepts `--browser-context` to pin signed-in reads to one Edge account; it
  does not retry a different account after `ACCESS_DENIED`;
- never exports cookies, session tokens or other credentials;
- reports `ACCESS_DENIED` when the signed-in account cannot open the
  conversation, and `CONTENT_NOT_RENDERED` when the page yields no transcript.

`coverage.complete` is always `false` for web capture: the DOM view is what the
page rendered in this session, not the workspace's stored record.

## Strict search and paging

`find --session <uuid> --literal <text>` is an exhaustive literal query over the declared native Codex scope. It returns native spans, a source snapshot hash and an explicit `complete`/`partial` status; it does not reuse the radar's truncated excerpt index.

`read --budget <chars>` returns one page of the logical event stream with an opaque `next_cursor`. Cursors are bound to the source snapshot and to the selection: a retried cursor returns the same page, consecutive cursors do not overlap or skip text, and a modified source returns `SOURCE_CHANGED` instead of silently changing the page. No read ledger is persisted.

Run `python scripts/session_inspect.py <mode> --help` for mode-specific options.
