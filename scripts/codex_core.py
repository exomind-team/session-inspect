"""Shared Codex JSONL core.

This module is the single place for Codex source identity, JSONL framing,
message-body extraction, mirror relations, ordering metadata and coverage
statistics. ``scan_codex`` (the fast radar) and ``extract_codex`` (read /
handoff) must both use it so their identity and body semantics cannot drift.

No persistent index, cache or database is created here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


TEXT_BLOCK_TYPES = ("input_text", "output_text", "text")
TOOL_PAYLOAD_TYPES = (
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "local_shell_call",
    "web_search_call",
)
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def id_from_filename(path) -> str:
    """Return a UUID-shaped identity from a filename, else the old stem rule."""
    name = Path(path).name
    match = UUID_RE.search(name)
    if match:
        return match.group(0)
    stem = Path(path).stem
    parts = stem.split("-")
    if len(parts) >= 6:
        return "-".join(parts[-5:])
    return ""


def normalize_for_match(text: str) -> str:
    return " ".join((text or "").split())


def content_text(content: Any) -> str:
    """Join only readable text blocks; unknown blocks stay out of the body."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in TEXT_BLOCK_TYPES:
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif block.get("type") == "refusal":
            text = block.get("refusal")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _read_zstd(path: Path) -> Tuple[Optional[bytes], str]:
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress(path.read_bytes()), "stdlib"
    except ImportError:
        pass
    except Exception:
        return None, "decoder-error"
    try:
        import zstandard  # optional third-party module

        with path.open("rb") as f:
            return zstandard.ZstdDecompressor().decompress(f.read()), "zstandard"
    except ImportError:
        return None, "unavailable"
    except Exception:
        return None, "decoder-error"


def _iter_full_lines(path: Path) -> Iterator[Tuple[int, str, bool]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for line_no, line in enumerate(f, 1):
            yield line_no, line, line.endswith(("\n", "\r"))


def _chunk_lines(
    chunk: bytes,
    start_position: int,
    *,
    trim_start: bool = False,
    trim_end: bool = False,
) -> Iterator[Tuple[int, str, bool]]:
    text = chunk.decode("utf-8", errors="ignore")
    rows = text.splitlines(keepends=True)
    if trim_start and rows and not rows[0].endswith(("\n", "\r")):
        rows = rows[1:]
    if trim_end and rows and not rows[-1].endswith(("\n", "\r")):
        rows = rows[:-1]
    for offset, line in enumerate(rows):
        yield start_position + offset, line, line.endswith(("\n", "\r"))


def _read_windows(path: Path, sample_bytes: int) -> List[Tuple[int, str, bool]]:
    size = path.stat().st_size
    rows: List[Tuple[int, str, bool]] = []
    with path.open("rb") as f:
        head = f.read(sample_bytes)
        tail_start = max(0, size - sample_bytes)
        tail = b""
        if tail_start > len(head):
            f.seek(tail_start)
            tail = f.read(sample_bytes)
    rows.extend(_chunk_lines(head, 1, trim_end=True))
    if tail:
        rows.extend(_chunk_lines(tail, -1, trim_start=True))
    return rows


def _new_state() -> Dict[str, Any]:
    return {
        "session_id": "",
        "cwd": "",
        "title": "",
        "session_info": {},
        "events": [],
        "tool_uses": Counter(),
        "compacted_summaries": [],
        "turn_aborted": 0,
        "malformed_lines": 0,
        "incomplete_tail": False,
        "record_count": 0,
    }


def _parse_session_meta(obj: Dict[str, Any], state: Dict[str, Any]) -> None:
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return
    git = payload.get("git", {}) or {}
    state["session_id"] = payload.get("id") or state["session_id"]
    state["cwd"] = payload.get("cwd") or state["cwd"]
    state["session_info"] = {
        "sessionId": state["session_id"],
        "cwd": payload.get("cwd", ""),
        "branch": git.get("branch", ""),
        "commit": git.get("commit_hash", ""),
        "repo_url": git.get("repository_url", ""),
        "model": payload.get("model_provider", ""),
        "timestamp": payload.get("timestamp", ""),
    }


def _add_event(
    state: Dict[str, Any],
    *,
    role: str,
    text: str,
    timestamp: str,
    channel: str,
    event_type: str,
    position: int,
    native_id: str = "",
    is_task_complete: bool = False,
) -> None:
    state["events"].append({
        "role": role,
        "text": text,
        "timestamp": timestamp,
        "channel": channel,
        "event_type": event_type,
        "position": position,
        "native_id": native_id,
        "is_task_complete": is_task_complete,
        "mirrored": False,
        "mirror_of_position": None,
    })


def _parse_event_msg(
    obj: Dict[str, Any], state: Dict[str, Any], position: int
) -> None:
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return
    event_type = payload.get("type", "")
    timestamp = obj.get("timestamp", "")
    if event_type == "user_message":
        text = payload.get("message", "")
        if isinstance(text, str) and text.strip():
            _add_event(state, role="user", text=text, timestamp=timestamp,
                       channel="event_msg", event_type=event_type, position=position)
    elif event_type == "agent_message":
        text = payload.get("message", "")
        if isinstance(text, str) and text.strip():
            _add_event(state, role="assistant", text=text, timestamp=timestamp,
                       channel="event_msg", event_type=event_type, position=position)
    elif event_type == "task_complete":
        text = payload.get("last_agent_message", "")
        if isinstance(text, str) and text.strip():
            _add_event(state, role="assistant", text=text, timestamp=timestamp,
                       channel="event_msg", event_type=event_type, position=position,
                       is_task_complete=True)
    elif event_type == "turn_aborted":
        state["turn_aborted"] += 1
    elif event_type == "context_compacted":
        replacement = payload.get("replacement_history", "")
        if replacement:
            if not isinstance(replacement, str):
                replacement = json.dumps(replacement, ensure_ascii=False)
            state["compacted_summaries"].append(replacement[:500])


def _parse_response_item(
    obj: Dict[str, Any], state: Dict[str, Any], position: int
) -> None:
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return
    payload_type = payload.get("type", "")
    timestamp = obj.get("timestamp", "")
    native_id = str(obj.get("id") or payload.get("id") or "")

    if payload_type == "message":
        role = payload.get("role", "")
        if role not in ("user", "assistant", "system", "developer"):
            return
        text = content_text(payload.get("content"))
        if text.strip():
            normalized_role = "assistant" if role == "assistant" else (
                "user" if role == "user" else "system")
            _add_event(state, role=normalized_role, text=text, timestamp=timestamp,
                       channel="response_item", event_type="message",
                       position=position, native_id=native_id)
    elif payload_type in TOOL_PAYLOAD_TYPES:
        name = payload.get("name") or payload_type
        if name:
            state["tool_uses"][str(name)] += 1
    elif payload_type == "compacted":
        replacement = payload.get("replacement_history", "")
        if replacement:
            if not isinstance(replacement, str):
                replacement = json.dumps(replacement, ensure_ascii=False)
            state["compacted_summaries"].append(replacement[:500])


def _consume_line(
    line: str,
    state: Dict[str, Any],
    position: int,
    terminated: bool,
) -> None:
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        if not terminated:
            state["incomplete_tail"] = True
        else:
            state["malformed_lines"] += 1
        return
    if not isinstance(obj, dict):
        state["malformed_lines"] += 1
        return
    state["record_count"] += 1
    record_type = obj.get("type", "")
    if record_type == "session_meta":
        _parse_session_meta(obj, state)
    elif record_type == "event_msg":
        _parse_event_msg(obj, state, position)
    elif record_type == "response_item":
        _parse_response_item(obj, state, position)
    elif record_type == "turn_context":
        payload = obj.get("payload", {})
        model = payload.get("model", "") if isinstance(payload, dict) else ""
        if model:
            state["session_info"]["last_model"] = model


def _mark_mirrors(events: List[Dict[str, Any]]) -> int:
    """Mark response_item messages that mirror an event_msg occurrence.

    Matching is occurrence-based on (role, normalized text), never a global
    text deduplication: two real identical messages still count twice.
    """
    pool: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for event in events:
        if (event["channel"] == "event_msg"
                and event["role"] in ("user", "assistant")
                and not event["is_task_complete"]
                and event["text"]):
            key = (event["role"], normalize_for_match(event["text"]))
            pool.setdefault(key, []).append(event)
    mirrored = 0
    for event in events:
        if event["channel"] != "response_item":
            continue
        if event["role"] not in ("user", "assistant") or not event["text"]:
            continue
        key = (event["role"], normalize_for_match(event["text"]))
        bucket = pool.get(key)
        if bucket:
            twin = bucket.pop(0)
            event["mirrored"] = True
            event["mirror_of_position"] = twin["position"]
            mirrored += 1
    return mirrored


def _empty_coverage() -> Dict[str, Any]:
    return {
        "source_enumeration": "jsonl+jsonl.zst-discovery",
        "body_read": "full-jsonl-pass",
        "search_scope": "truncated-recent-excerpts",
        "message_counts": "observed-records-not-human-turns",
        "complete": False,
        "sampled_source_count": 0,
        "malformed_lines": 0,
        "incomplete_tail": False,
        "mirrored_response_items": 0,
        "compressed_sources": [],
        "channels": [],
    }


def _unreadable_source(path: Path, decoder: str) -> Dict[str, Any]:
    coverage = _empty_coverage()
    coverage["body_read"] = "unreadable"
    coverage["compressed_sources"] = [{
        "path": str(path),
        "decoder": decoder,
        "readable": False,
    }]
    return {
        "path": str(path),
        "sha256": "",
        "bytes": path.stat().st_size if path.exists() else 0,
        "compressed": True,
        "decoder": decoder,
        "session_id": id_from_filename(path),
        "cwd": "",
        "title": "",
        "session_info": {},
        "events": [],
        "tool_uses": {},
        "compacted_summaries": [],
        "turn_aborted": 0,
        "record_count": 0,
        "malformed_lines": 0,
        "incomplete_tail": False,
        "mirrored_response_items": 0,
        "coverage": coverage,
        "readable": False,
    }


def parse_source(
    path,
    *,
    sampled: bool = False,
    sample_bytes: int = 4 * 1024 * 1024,
) -> Dict[str, Any]:
    """Parse one Codex rollout into logical events plus coverage metadata."""
    path = Path(path)
    compressed = path.suffix.lower() == ".zst"
    decoder = ""
    rows: Iterable[Tuple[int, str, bool]]

    if compressed:
        data, decoder = _read_zstd(path)
        if data is None:
            return _unreadable_source(path, decoder)
        raw_text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
        rows = (
            (line_no, line, line.endswith(("\n", "\r")))
            for line_no, line in enumerate(raw_text.splitlines(keepends=True), 1)
        )
    elif sampled:
        rows = _read_windows(path, sample_bytes)
    else:
        rows = _iter_full_lines(path)

    state = _new_state()
    for position, line, terminated in rows:
        _consume_line(line, state, position, terminated)

    if not state["session_id"]:
        state["session_id"] = id_from_filename(path)

    mirrored = _mark_mirrors(state["events"])
    logical = [event for event in state["events"] if not event["mirrored"]]
    coverage = _empty_coverage()
    coverage["body_read"] = "head-tail" if sampled else "full-jsonl-pass"
    coverage["sampled_source_count"] = 1 if sampled else 0
    coverage["malformed_lines"] = state["malformed_lines"]
    coverage["incomplete_tail"] = state["incomplete_tail"]
    coverage["mirrored_response_items"] = mirrored
    coverage["channels"] = sorted({event["channel"] for event in logical})
    if compressed:
        coverage["compressed_sources"] = [{
            "path": str(path),
            "decoder": decoder,
            "readable": True,
        }]
    coverage["complete"] = (
        not sampled
        and state["malformed_lines"] == 0
        and not state["incomplete_tail"]
    )

    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "compressed": compressed,
        "decoder": decoder,
        "session_id": state["session_id"],
        "cwd": state["cwd"],
        "title": state["title"],
        "session_info": state["session_info"],
        "events": logical,
        "all_events": state["events"],
        "tool_uses": dict(state["tool_uses"]),
        "compacted_summaries": list(state["compacted_summaries"]),
        "turn_aborted": state["turn_aborted"],
        "record_count": state["record_count"],
        "malformed_lines": state["malformed_lines"],
        "incomplete_tail": state["incomplete_tail"],
        "mirrored_response_items": mirrored,
        "coverage": coverage,
        "readable": True,
    }


def first_timestamp(source: Dict[str, Any]) -> str:
    for event in source.get("events") or []:
        if event.get("timestamp"):
            return str(event["timestamp"])
    return ""


def merge_sources(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge rollouts of one logical thread.

    File order is decided by each source's first event timestamp, with path as
    a stable tie-breaker. Within a source the native append order is preserved.
    Exact duplicate files must be removed by the caller before this call.
    """
    ordered = sorted(
        sources,
        key=lambda source: (
            first_timestamp(source) or "9999",
            source.get("path") or "",
        ),
    )
    events: List[Dict[str, Any]] = []
    tool_uses: Counter = Counter()
    compacted: List[str] = []
    turn_aborted = 0
    for source in ordered:
        events.extend(source.get("events") or [])
        tool_uses.update(source.get("tool_uses") or {})
        compacted.extend(source.get("compacted_summaries") or [])
        turn_aborted += int(source.get("turn_aborted") or 0)
    coverage = _empty_coverage()
    coverage["body_read"] = (
        "head-tail" if any((s.get("coverage") or {}).get("body_read") == "head-tail"
                           for s in ordered) else "full-jsonl-pass"
    )
    coverage["sampled_source_count"] = sum(
        int((s.get("coverage") or {}).get("sampled_source_count") or 0)
        for s in ordered)
    coverage["malformed_lines"] = sum(int(s.get("malformed_lines") or 0)
                                      for s in ordered)
    coverage["incomplete_tail"] = any(bool(s.get("incomplete_tail"))
                                      for s in ordered)
    coverage["mirrored_response_items"] = sum(
        int(s.get("mirrored_response_items") or 0) for s in ordered)
    coverage["channels"] = sorted(
        {channel for s in ordered
         for channel in (s.get("coverage") or {}).get("channels", [])})
    coverage["compressed_sources"] = [
        item for s in ordered
        for item in (s.get("coverage") or {}).get("compressed_sources", [])
    ]
    coverage["complete"] = (
        bool(ordered)
        and coverage["sampled_source_count"] == 0
        and coverage["malformed_lines"] == 0
        and not coverage["incomplete_tail"]
        and all(s.get("readable") for s in ordered)
    )
    return {
        "sources": ordered,
        "events": events,
        "tool_uses": dict(tool_uses),
        "compacted_summaries": compacted,
        "turn_aborted": turn_aborted,
        "coverage": coverage,
    }


def verify_sources(
    *,
    session_id: str,
    sources: List[Dict[str, Any]],
    merged: Dict[str, Any],
    product: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Structural verification for a native handoff product.

    This proves the serialized product matches the parsed core for the
    declared scope. It does not prove the source log itself is complete.
    """
    events = merged.get("events") or []
    user_count = sum(1 for event in events if event.get("role") == "user")
    agent_count = sum(1 for event in events if event.get("role") == "assistant")
    task_complete_count = sum(
        1 for event in events if event.get("is_task_complete"))
    conversation = (product or {}).get("conversations", [{}])
    conversation = conversation[0] if conversation else {}
    transcript = conversation.get("transcript") or []
    product_user = sum(1 for item in transcript if item.get("type") == "REQUEST")
    product_agent = sum(1 for item in transcript
                        if item.get("type") == "RESPONSE")
    checks = {
        "session_identity_present": bool(session_id),
        "source_files_readable": bool(sources) and all(
            source.get("readable") for source in sources),
        "source_hashes_present": bool(sources) and all(
            source.get("sha256") for source in sources),
        "user_message_count_matches": user_count == product_user,
        "agent_message_count_matches": agent_count == product_agent,
        "task_complete_count_matches": task_complete_count == sum(
            1 for item in transcript if item.get("is_task_complete")),
        "coverage_declared": all(source.get("coverage") for source in sources),
        "no_incomplete_tail": all(not source.get("incomplete_tail")
                                  for source in sources),
        "no_malformed_lines": all(int(source.get("malformed_lines") or 0) == 0
                                  for source in sources),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "scope": "native Codex file framing, body channels and declared coverage",
        "sources": [
            {
                "path": source.get("path"),
                "sha256": source.get("sha256"),
                "coverage": source.get("coverage"),
            }
            for source in sources
        ],
    }
