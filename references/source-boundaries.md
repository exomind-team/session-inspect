# Source boundaries

| Source | Discovery path | Preserved structure | Known limits |
|---|---|---|---|
| Codex local history | `overview`; exact session lookup; targeted extraction | JSONL `event_msg` and `response_item` bodies, per-source SHA-256, coverage stats | The quick overview is a radar view and may sample large rollouts; it declares `coverage.complete: false` and never claims exhaustive search. Compressed `.jsonl.zst` sources are discovered even when no decoder is available, and are then reported as unreadable. |
| Codex exact identity | `--session <uuid>`, `overview --session` | `session_meta.payload.id`, all matching rollout files | Filenames are never proof of identity; an explicit id bypasses the default `--days` window. |
| Codex message bodies | `read` / `handoff` | Full `event_msg` and `response_item` text with native ids | `response_item` messages mirroring an `event_msg` occurrence are counted once and declared via `mirrored_response_items`; identical repeated messages remain distinct events. |
| Claude Code local history | `overview`; targeted extraction | JSONL messages, tool blocks, sidecar/session metadata where supported | Project/session aggregation differs from Codex thread aggregation. |
| DeepSeek account export | `find --scan`; `read --platform deepseek` | `conversations.json` mapping tree and native node/fragment data | Export bundles may contain attachment metadata without bytes and omit some tool payloads or reference mappings. |
| DeepSeek share link | `read --platform deepseek-share` | Shared final-state messages, references, and available attachments | It represents the shared snapshot; earlier edited/deleted turns may be absent. |

A cross-source merge is a union of available evidence, not proof that either source is complete. Preserve per-node source labels and unknowns. Do not infer a citation-to-result mapping when the source format does not carry that relation.
