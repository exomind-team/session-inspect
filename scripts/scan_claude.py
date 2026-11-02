"""
Claude Code session scanner for unified session-review.

Preserves the original project-oriented aggregation while exposing the same
record shape as the Codex adapter.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from scan_common import (
    beautify_project_name,
    compact_path,
    extract_text_from_claude_content,
    is_noise,
    make_record,
    matches_filters,
    parse_time,
    short_cwd,
    truncate,
)


def projects_dir(home=None):
    home = Path(home).expanduser() if home else Path.home()
    return home / ".claude" / "projects"


def iter_claude_files(home=None):
    root = projects_dir(home)
    if root.exists():
        yield from root.rglob("*.jsonl")


def find_session_files(session_id, platform="all", home=None):
    if platform not in ("all", "claude"):
        return []
    matches = []
    for path in iter_claude_files(home):
        if session_id in path.name:
            matches.append(path)
    return matches


def scan(days=7, project=None, grep=None, session_id=None, home=None):
    if session_id:
        files = find_session_files(session_id, platform="claude", home=home)
        records = []
        for path in files:
            parsed = parse_claude_file(path)
            if parsed:
                records.append(_record_from_single_session(parsed, path))
        records = [r for r in records if _days_ok(r, days) and matches_filters(r, project, grep)]
        records.sort(key=lambda r: r["last_active_ts"], reverse=True)
        return records

    groups = defaultdict(_new_group)
    for path in iter_claude_files(home):
        if not _file_mtime_in_days(path, days):
            continue
        parsed = parse_claude_file(path)
        if not parsed:
            continue
        key = path.parent.name
        _merge_group(groups[key], parsed, path)

    records = []
    for project_key, group in groups.items():
        latest = group["latest"] or {}
        cwd = latest.get("cwd") or group["cwd"]
        display = beautify_project_name(project_key)
        first_msg = latest.get("first_message", "")
        last_msg = latest.get("last_message", "") or first_msg
        grep_text = " ".join(group["all_user_messages"][-30:] + group["all_agent_messages"][-10:])
        record = make_record(
            platform="claude",
            id=latest.get("session_id") or project_key,
            title=latest.get("title") or display,
            cwd=cwd,
            display_name=display,
            last_active_dt=group["last_active_dt"],
            user_turns=group["user_turns"],
            agent_turns=group["agent_turns"],
            session_count=group["session_count"],
            total_size_bytes=group["total_size_bytes"],
            first_message=first_msg,
            last_message=last_msg,
            grep_text=grep_text,
            source_path=group["source_path"],
        )
        record["workspace"] = short_cwd(cwd)
        record["cwd_compact"] = compact_path(cwd)
        if _days_ok(record, days) and matches_filters(record, project, grep):
            records.append(record)

    records.sort(key=lambda r: r["last_active_ts"], reverse=True)
    return records


def parse_claude_file(path):
    data = {
        "session_id": path.stem,
        "title": "",
        "cwd": "",
        "branch": "",
        "version": "",
        "last_active_dt": None,
        "user_turns": 0,
        "agent_turns": 0,
        "user_messages": [],
        "agent_messages": [],
        "total_size_bytes": path.stat().st_size,
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = parse_time(obj.get("timestamp"))
                if ts and (data["last_active_dt"] is None or ts > data["last_active_dt"]):
                    data["last_active_dt"] = ts
                typ = obj.get("type", "")
                if typ == "user":
                    _parse_user(obj, data)
                elif typ == "assistant":
                    _parse_assistant(obj, data)
                elif typ == "custom-title":
                    title = obj.get("customTitle", "")
                    if title:
                        data["title"] = title
                elif typ == "agent-name" and not data["title"]:
                    title = obj.get("agentName", "")
                    if title:
                        data["title"] = title
    except OSError:
        return None
    if data["last_active_dt"] is None:
        data["last_active_dt"] = parse_time(path.stat().st_mtime)
    return data


def _parse_user(obj, data):
    data["session_id"] = obj.get("sessionId") or data["session_id"]
    data["cwd"] = obj.get("cwd") or data["cwd"]
    data["branch"] = obj.get("gitBranch") or data["branch"]
    data["version"] = obj.get("version") or data["version"]
    text = extract_text_from_claude_content(obj.get("message", {}).get("content", ""))
    if text and not is_noise(text):
        data["user_turns"] += 1
        data["user_messages"].append(truncate(text, 2000))


def _parse_assistant(obj, data):
    data["agent_turns"] += 1
    content = obj.get("message", {}).get("content", [])
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    texts.append(text)
        if texts:
            data["agent_messages"].append(truncate("\n".join(texts), 2000))


def _record_from_single_session(parsed, path):
    display = parsed.get("title") or beautify_project_name(path.parent.name)
    first_msg = parsed["user_messages"][0] if parsed["user_messages"] else ""
    last_msg = parsed["user_messages"][-1] if len(parsed["user_messages"]) > 1 else first_msg
    record = make_record(
        platform="claude",
        id=parsed["session_id"],
        title=parsed.get("title") or display,
        cwd=parsed.get("cwd", ""),
        display_name=display,
        last_active_dt=parsed["last_active_dt"],
        user_turns=parsed["user_turns"],
        agent_turns=parsed["agent_turns"],
        session_count=1,
        total_size_bytes=parsed["total_size_bytes"],
        first_message=first_msg,
        last_message=last_msg,
        grep_text=" ".join(parsed["user_messages"] + parsed["agent_messages"]),
        source_path=str(path),
    )
    record["workspace"] = short_cwd(parsed.get("cwd", ""))
    record["cwd_compact"] = compact_path(parsed.get("cwd", ""))
    return record


def _new_group():
    return {
        "cwd": "",
        "last_active_dt": None,
        "user_turns": 0,
        "agent_turns": 0,
        "session_count": 0,
        "total_size_bytes": 0,
        "latest": None,
        "all_user_messages": [],
        "all_agent_messages": [],
        "source_path": "",
    }


def _merge_group(group, parsed, path):
    group["session_count"] += 1
    group["total_size_bytes"] += parsed["total_size_bytes"]
    group["user_turns"] += parsed["user_turns"]
    group["agent_turns"] += parsed["agent_turns"]
    group["all_user_messages"].extend(parsed["user_messages"])
    group["all_agent_messages"].extend(parsed["agent_messages"])
    if parsed["cwd"]:
        group["cwd"] = parsed["cwd"]
    if group["last_active_dt"] is None or parsed["last_active_dt"] > group["last_active_dt"]:
        group["last_active_dt"] = parsed["last_active_dt"]
        group["latest"] = {
            "session_id": parsed["session_id"],
            "title": parsed["title"],
            "cwd": parsed["cwd"],
            "first_message": parsed["user_messages"][0] if parsed["user_messages"] else "",
            "last_message": parsed["user_messages"][-1] if parsed["user_messages"] else "",
        }
        group["source_path"] = str(path)


def _days_ok(record, days):
    return days <= 0 or record["days_since_active"] <= days


def _file_mtime_in_days(path, days):
    if days <= 0:
        return True
    cutoff = datetime.now() - timedelta(days=days + 1)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) >= cutoff
    except OSError:
        return False
