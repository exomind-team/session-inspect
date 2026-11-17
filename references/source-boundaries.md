# Source boundaries

| Source | Discovery path | Preserved structure | Known limits |
|---|---|---|---|
| Codex local history | `overview`; exact session lookup; targeted extraction | JSONL `event_msg` and `response_item` bodies, per-source SHA-256, coverage stats | The quick overview is a radar view and may sample large rollouts; it declares `coverage.complete: false` and never claims exhaustive search. Compressed `.jsonl.zst` sources are discovered even when no decoder is available, and are then reported as unreadable. |
| Codex exact identity | `--session <uuid>`, `overview --session` | `session_meta.payload.id`, all matching rollout files | Filenames are never proof of identity; an explicit id bypasses the default `--days` window. |
| Codex message bodies | `read` / `handoff` | Full `event_msg` and `response_item` text with native ids | `response_item` messages mirroring an `event_msg` occurrence are counted once and declared via `mirrored_response_items`; identical repeated messages remain distinct events. |
| Claude Code local history | `overview`; targeted extraction | JSONL messages, tool blocks, sidecar/session metadata where supported | Project/session aggregation differs from Codex thread aggregation. |
| DeepSeek account export | `find --scan`; `read --platform deepseek` | `conversations.json` mapping tree and native node/fragment data | Export bundles may contain attachment metadata without bytes and omit some tool payloads or reference mappings. |
| DeepSeek share link | `read --platform deepseek-share` | Shared final-state messages, references, and available attachments | It represents the shared snapshot; earlier edited/deleted turns may be absent. |
| DeepSeek signed-in private session | `read --platform deepseek-private` | `chat_messages[]` + `parent_id` tree, full branch set, latest state | Requires a signed-in browser and the CDP proxy. External HTTP is blocked by the WAF (an HTTP 200 HTML page, not a 4xx). Token lives only in memory. See [deepseek-private.md](deepseek-private.md). |
| ChatGPT share link | `read --platform chatgpt-share` | Rendered DOM messages, listed images/files, attachment bytes fetched on request | Content is fetched client-side; a plain HTTP request only returns a shell. The share view is a snapshot, not the live conversation, and may omit attachments. |
| ChatGPT signed-in conversation | `read --platform chatgpt` | Rendered DOM messages in the signed-in browser session | Requires the account/workspace to have access. Reasoning, tool payloads and unrendered branches are out of scope. Never exports credentials. |

A cross-source merge is a union of available evidence, not proof that either source is complete. Preserve per-node source labels and unknowns. Do not infer a citation-to-result mapping when the source format does not carry that relation.

## Attachment bytes: a known boundary

Not every attachment that is *listed* can be *fetched*. Report the distinction explicitly
rather than writing a silent zero.

| Case | Behaviour |
|---|---|
| DeepSeek export bundle | Metadata only (`file_id` / `file_name` / `file_size`). The export zip carries no attachment bytes. |
| DeepSeek share link, image (`is_image: true`) | `files.deepseeksvc.com/api<signed_path>&ty=p` returns the bytes. A thumbnail is also available via `ty=t`. |
| DeepSeek share link, **document** | Returns **HTTP 400 for every `ty` value** (`p` / `t` / `d` / `f`), with or without auth. Not fetchable. Mark `bytes_available_reason: "document-not-downloadable"` and say a manual download is required. |
| DeepSeek signed-in private session | `signed_path` returns **400 for anonymous GET**. Bytes require the signed-in download control. |

Two further traps:

- **Same-name attachments exist.** Two different files named `frame_0.5s.png` can sit under
  one node. Write by `n<node>_<global_seq>_<name>`; naming by `file_name` alone **silently
  overwrites** and the manifest's `sha256` then disagrees with the file on disk.
- **Declared size vs actual bytes.** CDN re-encoding shrinks images legitimately, but a
  large shortfall usually means a truncated or blocked download. Record
  `size_matches_declared` / `size_ratio` and warn below 50%.

## Cross-source comparison

Comparing two captures of the same conversation with a raw fragment-signature diff produces
**false positives**: derived fields (`url_normalized`, `url_anchor`), per-request signed
paths, and citation anchors baked into `url` all drift between captures. Use
`deepseek_common.frag_signature_normalized`, which strips derived fields, volatile payloads,
and normalises result URLs first. Reserve the raw `frag_signature` for single-source
self-verification, where any difference is meaningful.
