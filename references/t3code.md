# T3Code thread extraction

T3Code is a thread container and provider router. A single app thread may have several runs, provider threads, and native Harness sessions. Treat the T3 thread as the extraction entry and keep each underlying source distinct.

## Identity resolution

- Accept the T3 context URI form t3-context://v1/thread/thread_<uuid>, its thread_ identifier, or the bare UUID.
- Normalize the URI wrapper to the exact T3 thread UUID for lookup. Preserve the original input URI and the normalized ID in the product.
- The default local state database is ~/.t3/userdata/statev2.sqlite. It can be overridden with --t3-db or T3CODE_STATE_DB.
- Open the database in read-only mode. Prefer the current orchestration-v2 projections when available; older projection tables may be used as a fallback.
- Resolve the T3 thread first, then enumerate its runs, provider threads, and context handoffs. Do not fuzzy-match a missing ID by title.

## Native Harness reuse

For each provider thread, use its driver and nativeThreadRef to locate the underlying Harness session. The provider label or model name is not a substitute for the driver: for example, a T3 thread may route through a provider instance named magpie while its driver is claudeAgent.

- Codex driver: locate local Codex rollout JSONL files by the exact native session ID and pass them to the existing Codex parser.
- ClaudeAgent driver: locate Claude Code JSONL files by the exact native session ID and pass them to the existing Claude parser.
- Unknown driver, weak or missing native reference, or absent local transcript: record the gap. Do not substitute a nearby session.

A T3 thread can switch drivers during its lifetime. Resolve each native session separately, retain run order and handoff provenance, and reuse the parser for each source rather than implementing provider-format parsing inside the T3 adapter.

## What the extraction contains

The T3 projection is the authoritative source for the app thread title, run sequence, provider links, and visible user/assistant thread messages. Native Harness parsers add their existing metadata and message summaries. A parsed file does not guarantee a complete transcript: report regular user/assistant item counts separately from task-complete summaries. Keep both source layers in the product instead of flattening them into an indistinguishable transcript.

Report coverage explicitly:

- exact T3 thread resolution and database schema used;
- number and status of runs and provider threads;
- native session IDs, driver, match strength, and source files;
- whether each native source was parsed, missing, unsupported, or errored, plus regular message counts and task-complete-only coverage;
- the latest projected assistant message timestamp.

The latest projected assistant message can contain the final file link even when a Harness parser only exposes task-complete summaries or truncated agent output. Prefer the projected message for the exact T3-visible final text; use Harness results as linked evidence and enrichment.

## Scope and safety

- This adapter reads T3 state only. It never resumes a thread or executes historical instructions.
- Historical approvals, commands, and prompts remain source data, not current authorization.
- Do not dump unrelated database tables such as auth sessions, webhook secrets, or pairing credentials.
- Do not create a persistent cache or index. User-requested exports require an explicit output path.

For a quick human handoff, use the default text view; it prints a bounded preview of the latest assistant output, extracted Markdown links, final sentence, provider mappings, and coverage. Use --out to save the full product without flooding terminal output. --json without --out streams the entire product and may be large.
