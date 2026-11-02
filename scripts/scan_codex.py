"""
Codex session scanner for unified session-review.

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
            if current is None or (updated and updated >= current.get("updated_at_dt")):
                titles[sid] = {
                    "title": obj.get("thread_name", "") or sid,
                    "updated_at_dt": updated,
                }
    return {sid: data["title"] for sid, data in titles.items()}


def iter_codex_files(home=None):
    for root in codex_roots(home):
        if root.exists():
            yield from root.rglob("*.jsonl")


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
    """
    saw_meta = False
    meta_lines = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
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
    return not saw_meta and session_id in path.name


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
        group = groups[sid]
        _merge_group(group, summary, path)

    records = []
    cutoff_ok = _days_filter(days)
    for sid, group in groups.items():
        title = titles.get(sid) or group["title"] or sid
        display = title if title and title != sid else display_name_from_cwd(group["cwd"], sid)
        first_msg = group["user_messages"][0] if group["user_messages"] else ""
        last_msg = group["user_messages"][-1] if len(group["user_messages"]) > 1 else first_msg
        grep_text = " ".join(group["user_messages"][-20:] + group["agent_messages"][-10:])
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
        record["workspace"] = short_cwd(group["cwd"])
        record["cwd_compact"] = compact_path(group["cwd"])
        if cutoff_ok(record) and matches_filters(record, project=project, grep=grep):
            records.append(record)

    records.sort(key=lambda r: r["last_active_ts"], reverse=True)
    return records


def parse_codex_file(path):
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > FULL_PARSE_MAX_BYTES:
        return parse_codex_file_sampled(path, size)

    data = {
        "id": "",
        "title": "",
        "cwd": "",
        "last_active_dt": None,
        "user_turns": 0,
        "agent_turns": 0,
        "user_messages": [],
        "agent_messages": [],
        "total_size_bytes": size,
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"session_meta"' not in line and '"event_msg"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = parse_time(obj.get("timestamp"))
                if ts and (data["last_active_dt"] is None or ts > data["last_active_dt"]):
                    data["last_active_dt"] = ts
                typ = obj.get("type")
                if typ == "session_meta":
                    _parse_meta(obj, data)
                elif typ == "event_msg":
                    _parse_event(obj, data)
                # Intentionally skip response_item/reasoning/tool output. Codex
                # rollouts can contain encrypted reasoning, screenshots, and
                # large tool outputs; event_msg has the readable scan surface.
    except OSError:
        return None
    if not data["id"]:
        data["id"] = _id_from_filename(path)
    if not data["id"]:
        return None
    if data["last_active_dt"] is None:
        data["last_active_dt"] = parse_time(path.stat().st_mtime)
    return data


def parse_codex_file_sampled(path, size):
    """Parse only head and tail windows for very large Codex rollouts."""
    data = {
        "id": "",
        "title": "",
        "cwd": "",
        "last_active_dt": None,
        "user_turns": 0,
        "agent_turns": 0,
        "user_messages": [],
        "agent_messages": [],
        "total_size_bytes": size,
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as text_file:
            first_line = text_file.readline()
            if first_line:
                _parse_relevant_line(first_line, data)
        with path.open("rb") as f:
            head = f.read(SAMPLE_BYTES)
            if size > SAMPLE_BYTES:
                f.seek(max(0, size - SAMPLE_BYTES))
                tail = f.read(SAMPLE_BYTES)
            else:
                tail = b""
    except OSError:
        return None

    for chunk in (head, tail):
        if not chunk:
            continue
        for raw_line in chunk.decode("utf-8", errors="ignore").splitlines():
            if '"session_meta"' not in raw_line and '"event_msg"' not in raw_line:
                continue
            _parse_relevant_line(raw_line, data)

    if not data["id"]:
        data["id"] = _id_from_filename(path)
    if not data["id"]:
        return None
    if data["last_active_dt"] is None:
        data["last_active_dt"] = parse_time(path.stat().st_mtime)
    return data


def _parse_relevant_line(line, data):
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    ts = parse_time(obj.get("timestamp"))
    if ts and (data["last_active_dt"] is None or ts > data["last_active_dt"]):
        data["last_active_dt"] = ts
    typ = obj.get("type")
    if typ == "session_meta":
        _parse_meta(obj, data)
    elif typ == "event_msg":
        _parse_event(obj, data)


def _parse_meta(obj, data):
    payload = obj.get("payload", {})
    data["id"] = payload.get("id") or data["id"]
    data["cwd"] = payload.get("cwd") or data["cwd"]
    meta_ts = parse_time(payload.get("timestamp"))
    if meta_ts and (data["last_active_dt"] is None or meta_ts > data["last_active_dt"]):
        data["last_active_dt"] = meta_ts


def _parse_event(obj, data):
    payload = obj.get("payload", {})
    etype = payload.get("type", "")
    message = payload.get("message", "")
    if etype == "user_message":
        if isinstance(message, str) and message.strip() and not is_noise(message):
            data["user_turns"] += 1
            data["user_messages"].append(truncate(message, 2000))
    elif etype == "agent_message":
        if isinstance(message, str) and message.strip():
            data["agent_turns"] += 1
            data["agent_messages"].append(truncate(message, 2000))
    elif etype == "task_complete":
        text = payload.get("last_agent_message", "")
        if isinstance(text, str) and text.strip():
            data["agent_turns"] += 1
            data["agent_messages"].append(truncate(text, 2000))


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
        "source_path": "",
    }


def _merge_group(group, summary, path):
    group["rollout_count"] += 1
    group["total_size_bytes"] += summary["total_size_bytes"]
    group["user_turns"] += summary["user_turns"]
    group["agent_turns"] += summary["agent_turns"]
    group["user_messages"].extend(summary["user_messages"])
    group["agent_messages"].extend(summary["agent_messages"])
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
