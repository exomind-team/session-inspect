"""
Codex session scanner for session-inspect overview.

Scans ~/.codex/sessions and ~/.codex/archived_sessions, groups rollout JSONL
files by thread id, and only reads safe/structured fields.
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from scan_common import (
    compact_path,
    display_name_from_cwd,
    is_noise,
    make_record,
    matches_filters,
    parse_time,
    short_cwd,
    truncate,
)
import codex_core


FULL_PARSE_MAX_BYTES = 20 * 1024 * 1024
SAMPLE_BYTES = 4 * 1024 * 1024
SESSION_META_LOOKUP_LINES = 8


def codex_roots(home=None):
    home = Path(home).expanduser() if home else Path.home()
    codex = home / ".codex"
    return [
        codex / "sessions",
        codex / "archived_sessions",
    ]


def load_thread_titles(home=None):
    """Return latest title by thread id from ~/.codex/session_index.jsonl."""
    home = Path(home).expanduser() if home else Path.home()
    index_path = home / ".codex" / "session_index.jsonl"
    titles = {}
    if not index_path.exists():
        return titles
    with index_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = obj.get("id", "")
            if not sid:
                continue
            current = titles.get(sid)
            updated = parse_time(obj.get("updated_at"))
            if current is None or (
                updated is not None
                and (current.get("updated_at_dt") is None
                     or updated >= current["updated_at_dt"])
            ):
                titles[sid] = {
                    "title": obj.get("thread_name", "") or sid,
                    "updated_at_dt": updated,
                }
    return {sid: data["title"] for sid, data in titles.items()}


def iter_codex_files(home=None):
    for root in codex_roots(home):
        if root.exists():
            files = list(root.rglob("*.jsonl"))
            files.extend(root.rglob("*.jsonl.zst"))
            yield from sorted(set(files))


def find_session_files(session_id, platform="all", home=None):
    if platform not in ("all", "codex"):
        return []
    matches = []
    for path in iter_codex_files(home):
        if codex_file_has_primary_session(path, session_id):
            matches.append(path)
    return matches


def codex_file_has_primary_session(path, session_id):
    """Return true when a rollout's primary session_meta id matches session_id.

    UUIDs also appear in parent_thread_id/forked_from_id fields. Those are
    related sessions, not the primary thread identity for --session lookup.
    A filename is a discovery hint, never proof of primary identity.
    """
    path = Path(path)
    if path.suffix.lower() == ".zst":
        source = codex_core.parse_source(path)
        return bool(source.get("readable")
                    and source.get("session_id") == session_id)
    saw_meta = False
    meta_lines = 0
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if '"session_meta"' not in line:
                    if saw_meta:
                        break
                    continue
                saw_meta = True
                meta_lines += 1
                if _primary_id_from_session_meta_line(line) == session_id:
                    return True
                if meta_lines >= SESSION_META_LOOKUP_LINES:
                    break
    except OSError:
        return False
    return False


def _primary_id_from_session_meta_line(line):
    head = line[:4096]
    match = re.search(r'"payload"\s*:\s*\{\s*"id"\s*:\s*"([^"]+)"', head)
    if match:
        return match.group(1)
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if obj.get("type") != "session_meta":
        return ""
    payload = obj.get("payload", {})
    return payload.get("id", "")


def scan(days=7, project=None, grep=None, session_id=None, home=None):
    titles = load_thread_titles(home)
    groups = defaultdict(_new_group)
    wanted_files = None
    if session_id:
        wanted_files = set(find_session_files(session_id, platform="codex", home=home))
        if not wanted_files:
            return []

    seen_hashes = {}
    for path in iter_codex_files(home):
        if wanted_files is not None and path not in wanted_files:
            continue
        if wanted_files is None and not _file_mtime_in_days(path, days):
            continue
        summary = parse_codex_file(path)
        if summary is None:
            continue
        sid = summary["id"]
        if session_id and sid != session_id:
            continue
        digest = summary.get("sha256")
        if digest and digest in seen_hashes:
            group = groups[sid]
            group.setdefault("duplicate_paths", []).append(str(path))
            continue
        if digest:
            seen_hashes[digest] = path
        group = groups[sid]
        _merge_group(group, summary, path)

    records = []
    cutoff_ok = _days_filter(0 if session_id else days)
    for sid, group in groups.items():
        title = titles.get(sid) or group["title"] or sid
        display = title if title and title != sid else display_name_from_cwd(group["cwd"], sid)
        user_messages = _ordered_texts(group["user_events"], group["user_messages"])
        first_msg = user_messages[0] if user_messages else ""
        last_msg = user_messages[-1] if len(user_messages) > 1 else first_msg
        # The radar advertises truncated recent excerpts, so the grep surface
        # is deliberately bounded rather than silently treated as exhaustive.
        grep_text = " ".join(
            user_messages[-20:]
            + group["agent_messages"][-10:]
            + ([first_msg, last_msg] if first_msg or last_msg else [])
        )
        record = make_record(
            platform="codex",
            id=sid,
            title=title,
            cwd=group["cwd"],
            display_name=display,
            last_active_dt=group["last_active_dt"],
            user_turns=group["user_turns"],
            agent_turns=group["agent_turns"],
            rollout_count=group["rollout_count"],
            total_size_bytes=group["total_size_bytes"],
            first_message=first_msg,
            last_message=last_msg,
            grep_text=grep_text,
            source_path=group["source_path"],
        )
        record["coverage"] = {
            "source_enumeration": "jsonl+zst-discovery",
            "body_read": ("head-tail" if group["sampled_source_count"]
                          else ("unreadable" if group["unreadable_count"]
                                else "full-jsonl-pass")),
            "sampled_source_count": group["sampled_source_count"],
            "message_counts": "observed-records-not-human-turns",
            "search_scope": "truncated-recent-excerpts",
            "malformed_lines": group["malformed_lines"],
            "incomplete_tail": group["incomplete_tail"],
            "mirrored_response_items": group["mirrored_response_items"],
            "compressed_unreadable": list(group["compressed_unreadable"]),
            "identity_source": ("session-meta" if group["readable_count"]
                                else "filename-hint"),
            "complete": False,
        }
        record["source_paths"] = list(group["source_paths"])
        record["duplicate_source_paths"] = list(group["duplicate_paths"])
        record["workspace"] = short_cwd(group["cwd"])
        record["cwd_compact"] = compact_path(group["cwd"])
        if cutoff_ok(record) and _matches_with_bounded_rescan(
                record, group, project=project, grep=grep):
            records.append(record)

    records.sort(key=lambda r: r["last_active_ts"], reverse=True)
    return records


def _matches_with_bounded_rescan(record, group, *, project=None, grep=None):
    """Apply radar filters, rescanning stored texts before reporting no match.

    The radar still advertises truncated excerpts: this rescan only widens the
    literal check to text already parsed for the record, and records that it
    happened. It does not index the source or claim exhaustive coverage.
    """
    if matches_filters(record, project=project, grep=grep):
        return True
    if not grep or project and not matches_filters(record, project=project):
        return False
    needle = grep.lower()
    haystack = " ".join([
        record.get("first_message", ""),
        record.get("last_message", ""),
        record.get("_grep_text", ""),
    ] + [text for _, text in group.get("user_events", [])]
      + [text for _, text in group.get("agent_events", [])]).lower()
    if needle in haystack:
        record.setdefault("coverage", {})["search_scope"] = (
            "truncated-excerpts+literal-rescan")
        return True
    return False


def _ordered_texts(events, fallback):
    """Order messages by native event time, not by file enumeration order."""
    if not events:
        return list(fallback)
    ordered = sorted(
        enumerate(events),
        key=lambda item: (item[1][0] or "9999", item[0]),
    )
    return [text for _, (_, text) in ordered]


def parse_codex_file(path):
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > FULL_PARSE_MAX_BYTES:
        return parse_codex_file_sampled(path, size)
    return _summary_from_core(codex_core.parse_source(path))


def parse_codex_file_sampled(path, size):
    """Parse only head and tail windows for very large Codex rollouts."""
    return _summary_from_core(
        codex_core.parse_source(path, sampled=True, sample_bytes=SAMPLE_BYTES))


def _summary_from_core(source):
    """Adapt the shared core result to the radar's compact record shape."""
    if not source.get("readable"):
        # Keep the source visible without pretending we know its identity or
        # contents. The id here is only a filename hint for display.
        return {
            "id": source.get("session_id") or "",
            "title": "",
            "cwd": "",
            "last_active_dt": None,
            "user_turns": 0,
            "agent_turns": 0,
            "user_messages": [],
            "agent_messages": [],
            "user_events": [],
            "agent_events": [],
            "total_size_bytes": source.get("bytes") or 0,
            "sha256": source.get("sha256") or "",
            "sampled": False,
            "malformed_lines": 0,
            "incomplete_tail": False,
            "mirrored_response_items": 0,
            "compressed_unreadable": [
                item.get("path")
                for item in (source.get("coverage") or {}).get(
                    "compressed_sources", [])
            ],
            "coverage": source.get("coverage") or {},
            "readable": False,
            "identity_source": "filename-hint",
        }
    events = source.get("events") or []
    user_messages = []
    agent_messages = []
    user_events = []
    agent_events = []
    user_turns = 0
    agent_turns = 0
    last_active_dt = None
    for event in events:
        timestamp = parse_time(event.get("timestamp"))
        if timestamp and (last_active_dt is None or timestamp > last_active_dt):
            last_active_dt = timestamp
        text = event.get("text") or ""
        if not text.strip():
            continue
        role = event.get("role")
        if role == "user":
            if is_noise(text):
                continue
            user_turns += 1
            user_messages.append(truncate(text, 2000))
            user_events.append((event.get("timestamp") or "", text))
        elif role == "assistant":
            agent_turns += 1
            agent_messages.append(truncate(text, 2000))
            agent_events.append((event.get("timestamp") or "", text))
    session_id = source.get("session_id") or ""
    if not session_id:
        return None
    if last_active_dt is None:
        last_active_dt = parse_time(path_mtime(source["path"]))
    coverage = dict(source.get("coverage") or {})
    compressed_unreadable = [
        item.get("path") for item in coverage.get("compressed_sources", [])
        if not item.get("readable")
    ]
    return {
        "id": session_id,
        "title": source.get("title") or "",
        "cwd": source.get("cwd") or "",
        "last_active_dt": last_active_dt,
        "user_turns": user_turns,
        "agent_turns": agent_turns,
        "user_messages": user_messages,
        "agent_messages": agent_messages,
        "user_events": user_events,
        "agent_events": agent_events,
        "total_size_bytes": source.get("bytes") or 0,
        "sha256": source.get("sha256") or "",
        "sampled": (coverage.get("body_read") == "head-tail"),
        "malformed_lines": source.get("malformed_lines") or 0,
        "incomplete_tail": bool(source.get("incomplete_tail")),
        "mirrored_response_items": source.get("mirrored_response_items") or 0,
        "compressed_unreadable": compressed_unreadable,
        "coverage": coverage,
        "readable": True,
        "identity_source": "session-meta",
    }


def path_mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _id_from_filename(path):
    stem = path.stem
    parts = stem.split("-")
    if len(parts) >= 6:
        return "-".join(parts[-5:])
    return ""


def _new_group():
    return {
        "title": "",
        "cwd": "",
        "last_active_dt": None,
        "user_turns": 0,
        "agent_turns": 0,
        "rollout_count": 0,
        "total_size_bytes": 0,
        "user_messages": [],
        "agent_messages": [],
        "user_events": [],
        "agent_events": [],
        "source_path": "",
        "source_paths": [],
        "duplicate_paths": [],
        "sampled_source_count": 0,
        "malformed_lines": 0,
        "incomplete_tail": False,
        "mirrored_response_items": 0,
        "compressed_unreadable": [],
        "readable_count": 0,
        "unreadable_count": 0,
    }


def _merge_group(group, summary, path):
    group["source_paths"].append(str(path))
    if summary.get("readable", True):
        group["readable_count"] += 1
    else:
        group["unreadable_count"] += 1
    group["sampled_source_count"] += int(summary.get("sampled", False))
    group["malformed_lines"] += int(summary.get("malformed_lines") or 0)
    group["incomplete_tail"] = (group["incomplete_tail"]
                                or bool(summary.get("incomplete_tail")))
    group["mirrored_response_items"] += int(
        summary.get("mirrored_response_items") or 0)
    group["compressed_unreadable"].extend(
        summary.get("compressed_unreadable") or [])
    group["rollout_count"] += 1
    group["total_size_bytes"] += summary["total_size_bytes"]
    group["user_turns"] += summary["user_turns"]
    group["agent_turns"] += summary["agent_turns"]
    group["user_messages"].extend(summary["user_messages"])
    group["agent_messages"].extend(summary["agent_messages"])
    group["user_events"].extend(summary.get("user_events") or [])
    group["agent_events"].extend(summary.get("agent_events") or [])
    if summary["cwd"]:
        group["cwd"] = summary["cwd"]
    if group["last_active_dt"] is None or summary["last_active_dt"] > group["last_active_dt"]:
        group["last_active_dt"] = summary["last_active_dt"]
        group["source_path"] = str(path)


def _days_filter(days):
    if days <= 0:
        return lambda _record: True
    return lambda record: record["days_since_active"] <= days


def _file_mtime_in_days(path, days):
    if days <= 0:
        return True
    cutoff = datetime.now() - timedelta(days=days + 1)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) >= cutoff
    except OSError:
        return False
