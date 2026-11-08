"""Read-only adapter from T3Code threads to their provider-native sessions."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


SCHEMA = "session-inspect/t3-thread-v1"
MESSAGE_TYPES = {"user_message": "user", "assistant_message": "assistant"}


class T3Error(ValueError):
    """A T3 reference or local state database could not be resolved."""


def normalize_thread_ref(reference: str) -> tuple[str, str]:
    """Return (database UUID, canonical T3 context URI) for a T3 thread reference."""
    value = str(reference or "").strip()
    if not value:
        raise T3Error("T3 thread reference is empty")
    if value.startswith("t3-context://"):
        parsed = urlsplit(value)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if parsed.netloc != "v1" or len(parts) != 2 or parts[0] != "thread":
            raise T3Error("Unsupported T3 context URI; expected t3-context://v1/thread/<id>")
        value = parts[1]
        if value.startswith("thread_"):
            value = value[len("thread_"):]
    elif value.startswith("thread_"):
        value = value[len("thread_"):]
    if (not value or len(value) > 512
            or any(ord(char) < 33 for char in value)
            or any(char in value for char in ("/", "\\", "?", "#"))):
        raise T3Error("Invalid T3 thread identifier")
    return value, "t3-context://v1/thread/thread_" + quote(value, safe="")


def default_db_path(home=None):
    override = os.environ.get("T3CODE_STATE_DB")
    if override:
        return Path(override).expanduser()
    root = Path(home).expanduser() if home else Path.home()
    return root / ".t3" / "userdata" / "statev2.sqlite"


def _read_only_uri(path):
    path = Path(path).expanduser().resolve()
    return path.as_uri() + "?mode=ro"


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


class T3Store:
    """Small schema-aware, read-only view over T3Code's local SQLite projections."""

    def __init__(self, db_path=None, home=None):
        self.path = Path(db_path).expanduser() if db_path else default_db_path(home)
        if not self.path.is_file():
            raise T3Error("T3 state database not found: " + str(self.path))
        try:
            self.connection = sqlite3.connect(_read_only_uri(self.path), uri=True, timeout=5)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            self.tables = {r[0] for r in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            raise T3Error("Cannot open T3 state database read-only: " + str(exc)) from exc

    def close(self):
        self.connection.close()

    def _has(self, table):
        return table in self.tables

    def _all(self, sql, params=()):
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def _one(self, sql, params=()):
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_thread(self, reference):
        thread_id, context_uri = normalize_thread_ref(reference)
        if self._has("orchestration_v2_projection_threads"):
            thread = self._one(
                "SELECT * FROM orchestration_v2_projection_threads WHERE thread_id=?",
                (thread_id,))
            schema = "orchestration-v2"
        elif self._has("projection_threads"):
            thread = self._one(
                "SELECT * FROM projection_threads WHERE thread_id=?", (thread_id,))
            schema = "legacy-projection"
        else:
            raise T3Error("T3 database has no supported thread projection table")
        if not thread:
            raise T3Error("T3 thread not found: " + thread_id)
        thread["thread_id"] = thread_id
        thread["context_uri"] = context_uri
        thread["database_schema"] = schema
        thread["database_path"] = str(self.path)
        thread["payload"] = _json_object(thread.get("payload_json"))
        thread["project"] = self._get_project(thread.get("project_id"))
        thread["runs"] = self._get_runs(thread_id, schema)
        thread["provider_threads"] = self._get_provider_threads(thread_id, schema)
        thread["turn_items"] = self._get_turn_items(thread_id, schema)
        thread["messages"] = self._get_messages(thread_id, schema, thread["turn_items"])
        thread["context_handoffs"] = self._get_handoffs(thread_id, schema)
        return thread

    def _get_project(self, project_id):
        if not project_id or not self._has("projection_projects"):
            return {}
        row = self._one(
            "SELECT project_id,title,workspace_root,updated_at FROM projection_projects WHERE project_id=?",
            (project_id,))
        return row or {}

    def _get_runs(self, thread_id, schema):
        table = "orchestration_v2_projection_runs" if schema == "orchestration-v2" else None
        if not table or not self._has(table):
            return []
        rows = self._all(
            "SELECT run_id,ordinal,provider,provider_thread_id,status,requested_at,completed_at "
            "FROM orchestration_v2_projection_runs WHERE thread_id=? ORDER BY ordinal",
            (thread_id,))
        return rows

    def _get_provider_threads(self, thread_id, schema):
        table = "orchestration_v2_projection_provider_threads" if schema == "orchestration-v2" else None
        if not table or not self._has(table):
            return []
        if self._has("orchestration_v2_projection_provider_sessions"):
            session_columns = {item[1] for item in self.connection.execute(
                "PRAGMA table_info(orchestration_v2_projection_provider_sessions)")}
            model_column = "s.model AS provider_model" if "model" in session_columns else "NULL AS provider_model"
            payload_column = ("s.payload_json AS provider_session_payload"
                              if "payload_json" in session_columns
                              else "NULL AS provider_session_payload")
            session_join = "ON s.provider_session_id=p.provider_session_id"
            if "thread_id" in session_columns:
                session_join += " AND s.thread_id=p.thread_id"
            rows = self._all(
                "SELECT p.provider_thread_id,p.provider,p.provider_session_id,p.status,"
                "p.first_run_ordinal,p.last_run_ordinal,p.driver,p.provider_instance_id,"
                "p.payload_json," + model_column + "," + payload_column + " "
                "FROM orchestration_v2_projection_provider_threads p "
                "LEFT JOIN orchestration_v2_projection_provider_sessions s "
                + session_join + " "
                "WHERE p.thread_id=? ORDER BY p.first_run_ordinal",
                (thread_id,))
        else:
            rows = self._all(
                "SELECT provider_thread_id,provider,provider_session_id,status,first_run_ordinal,"
                "last_run_ordinal,driver,provider_instance_id,payload_json "
                "FROM orchestration_v2_projection_provider_threads WHERE thread_id=? "
                "ORDER BY first_run_ordinal",
                (thread_id,))
        for row in rows:
            payload = _json_object(row.pop("payload_json", ""))
            session_payload = _json_object(row.pop("provider_session_payload", ""))
            native = payload.get("nativeThreadRef") or {}
            row["native_driver"] = native.get("driver") or row.get("driver") or ""
            row["native_id"] = native.get("nativeId") or ""
            row["native_id_strength"] = native.get("strength") or "unknown"
            row["provider_model"] = (row.get("provider_model") or session_payload.get("model")
                                     or payload.get("model") or "")
            row.pop("model", None)
            row["handoff_ids"] = payload.get("handoffIds") or []
        return rows

    def _get_turn_items(self, thread_id, schema):
        if schema != "orchestration-v2" or not self._has("orchestration_v2_projection_turn_items"):
            return []
        rows = self._all(
            "SELECT turn_item_id,run_id,node_id,provider_thread_id,provider_turn_id,parent_item_id,"
            "ordinal,type,status,updated_at,payload_json "
            "FROM orchestration_v2_projection_turn_items WHERE thread_id=? ORDER BY ordinal",
            (thread_id,))
        items = []
        for row in rows:
            payload = _json_object(row.pop("payload_json", ""))
            item_type = row.get("type") or ""
            role = MESSAGE_TYPES.get(item_type, "")
            text = _payload_text(payload) if role else ""
            item = {
                "id": row.get("turn_item_id") or "",
                "ordinal": row.get("ordinal"),
                "run_id": row.get("run_id") or "",
                "node_id": row.get("node_id") or "",
                "provider_thread_id": row.get("provider_thread_id") or "",
                "provider_turn_id": row.get("provider_turn_id") or "",
                "type": item_type,
                "status": row.get("status") or "",
                "timestamp": payload.get("completedAt") or payload.get("startedAt")
                             or row.get("updated_at") or "",
                "role": role,
                "text": text,
                "native_item_ref": payload.get("nativeItemRef") or {},
                "title": payload.get("title") or "",
                "name": payload.get("name") or payload.get("toolName") or "",
            }
            items.append(item)
        return items

    def _get_messages(self, thread_id, schema, items):
        messages = [item for item in items if item.get("role") and item.get("text")]
        if messages:
            return messages
        if schema == "legacy-projection" and self._has("projection_thread_messages"):
            rows = self._all(
                "SELECT message_id,thread_id,turn_id,role,text,created_at,updated_at,"
                "attachments_json,context_json FROM projection_thread_messages "
                "WHERE thread_id=? ORDER BY created_at,message_id",
                (thread_id,))
            return [{
                "id": row.get("message_id") or "",
                "ordinal": None,
                "run_id": "",
                "node_id": "",
                "provider_thread_id": "",
                "provider_turn_id": row.get("turn_id") or "",
                "type": "legacy_message",
                "status": "stored",
                "timestamp": row.get("created_at") or row.get("updated_at") or "",
                "role": row.get("role") or "",
                "text": row.get("text") or "",
                "native_item_ref": {},
                "title": "",
                "name": "",
            } for row in rows]
        if schema == "orchestration-v2" and self._has("orchestration_v2_projection_messages"):
            rows = self._all(
                "SELECT message_id,run_id,node_id,role,created_at,updated_at,payload_json "
                "FROM orchestration_v2_projection_messages WHERE thread_id=? "
                "ORDER BY created_at,message_id", (thread_id,))
            out = []
            for row in rows:
                payload = _json_object(row.get("payload_json"))
                text = _payload_text(payload)
                if text:
                    out.append({
                        "id": row.get("message_id") or "",
                        "ordinal": None,
                        "run_id": row.get("run_id") or "",
                        "node_id": row.get("node_id") or "",
                        "provider_thread_id": "",
                        "provider_turn_id": "",
                        "type": "projection_message",
                        "status": "stored",
                        "timestamp": row.get("created_at") or row.get("updated_at") or "",
                        "role": row.get("role") or "",
                        "text": text,
                        "native_item_ref": {},
                        "title": "",
                        "name": "",
                    })
            return out
        return []

    def _get_handoffs(self, thread_id, schema):
        if schema != "orchestration-v2" or not self._has("orchestration_v2_projection_context_handoffs"):
            return []
        rows = self._all(
            "SELECT context_handoff_id,target_run_id,to_provider_thread_id,strategy,status,"
            "updated_at,payload_json FROM orchestration_v2_projection_context_handoffs "
            "WHERE thread_id=? ORDER BY updated_at", (thread_id,))
        out = []
        for row in rows:
            payload = _json_object(row.pop("payload_json", ""))
            out.append({
                "id": row.get("context_handoff_id") or "",
                "target_run_id": row.get("target_run_id") or "",
                "to_provider_thread_id": row.get("to_provider_thread_id") or "",
                "strategy": row.get("strategy") or "",
                "status": row.get("status") or "",
                "updated_at": row.get("updated_at") or "",
                "source_provider": payload.get("sourceProvider") or "",
                "target_provider": payload.get("targetProvider") or "",
            })
        return out


def _payload_text(payload):
    value = payload.get("text")
    if isinstance(value, str):
        return value
    value = payload.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            block.get("text", "") for block in value
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _last_sentence(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    link_pattern = re.compile(r"\[([^\]]*)\]\(\s*(?:<[^>]+>|[^\s)]+)\s*\)")
    for line in reversed(lines):
        leading_links = re.match(r"^(?:\[[^\]]*\]\(\s*(?:<[^>]+>|[^\s)]+)\s*\)\s*)+", line)
        if leading_links and line[leading_links.end():].strip():
            value = line[leading_links.end():].strip()
        else:
            value = link_pattern.sub(r"\1", line)
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            continue
        parts = [part.strip() for part in re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+", value)]
        parts = [part for part in parts if part]
        return parts[-1] if parts else value
    return ""


def _file_links(text):
    found = []
    pattern = re.compile(r"\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))\s*\)")
    for match in pattern.finditer(text or ""):
        link = (match.group(1) or match.group(2) or "").strip().strip("<>")
        if link and link not in found:
            found.append(link)
    return found


def latest_assistant(messages):
    for item in reversed(messages or []):
        if item.get("role") == "assistant" and item.get("text"):
            text = item["text"].strip()
            return {
                "item_id": item.get("id") or "",
                "timestamp": item.get("timestamp") or "",
                "text": text,
                "file_links": _file_links(text),
                "last_sentence": _last_sentence(text),
            }
    return {"item_id": "", "timestamp": "", "text": "", "file_links": [], "last_sentence": ""}

def extract_thread(reference, home=None, db_path=None, include_harness=True):
    """Extract a T3 thread and join each provider-native harness through existing parsers."""
    store = T3Store(db_path=db_path, home=home)
    try:
        thread = store.get_thread(reference)
    finally:
        store.close()

    latest = latest_assistant(thread["messages"])
    harness_sessions = _extract_harness_sessions(
        thread["provider_threads"], home=home) if include_harness else []
    found = sum(1 for item in harness_sessions if item.get("status") == "parsed")
    missing = sum(1 for item in harness_sessions
                  if item.get("status") in ("not_found", "missing_native_reference"))
    unsupported = sum(1 for item in harness_sessions if item.get("status") == "unsupported")
    parse_errors = sum(1 for item in harness_sessions if item.get("status") == "parse_error")
    task_only = sum(1 for item in harness_sessions
                    if item.get("content_coverage") == "task-complete-only")
    no_messages = sum(1 for item in harness_sessions
                      if item.get("content_coverage") == "no-readable-messages")
    thread_row = {
        "thread_id": thread["thread_id"],
        "context_uri": thread["context_uri"],
        "title": thread.get("title") or "",
        "project_id": thread.get("project_id") or "",
        "project_title": thread.get("project", {}).get("title") or "",
        "workspace_root": thread.get("project", {}).get("workspace_root") or "",
        "branch": thread.get("branch") or thread.get("payload", {}).get("branch") or "",
        "worktree_path": thread.get("worktree_path") or thread.get("payload", {}).get("worktreePath") or "",
        "default_provider": thread.get("default_provider") or "",
        "created_at": thread.get("created_at") or "",
        "updated_at": thread.get("updated_at") or "",
        "archived_at": thread.get("archived_at") or "",
        "deleted_at": thread.get("deleted_at") or "",
        "settled_override": thread.get("payload", {}).get("settledOverride") or "",
    }
    return {
        "_meta": {
            "schema": SCHEMA,
            "platform": "t3",
            "source": {
                "type": "t3-local-state",
                "uri": thread["context_uri"],
                "thread_id": thread["thread_id"],
                "database": thread["database_path"],
                "database_schema": thread["database_schema"],
                "resolution": "exact",
            },
            "coverage": {
                "thread_projection": "read",
                "runs": len(thread["runs"]),
                "provider_threads": len(thread["provider_threads"]),
                "projected_turn_items": len(thread["turn_items"]),
                "projected_messages": len(thread["messages"]),
                "native_harness_sessions_found": found,
                "native_harness_sessions_not_found": missing,
                "native_harness_sessions_unsupported": unsupported,
                "native_harness_sessions_parse_errors": parse_errors,
                "native_harness_sessions_task_complete_only": task_only,
                "native_harness_sessions_no_readable_messages": no_messages,
                "native_harness_included": bool(include_harness),
                "latest_assistant_timestamp": latest.get("timestamp") or "",
                "thread_updated_at": thread_row["updated_at"],
            },
        },
        "thread": thread_row,
        "runs": thread["runs"],
        "provider_threads": thread["provider_threads"],
        "context_handoffs": thread["context_handoffs"],
        "messages": thread["messages"],
        "turn_items": thread["turn_items"],
        "native_harness_sessions": harness_sessions,
        "latest_assistant": latest,
    }


def _extract_harness_sessions(provider_threads, home=None):
    import scan_claude
    import scan_codex
    from extract_claude import extract as parse_claude
    from extract_codex import extract as parse_codex

    parsed_by_key = {}
    for provider_thread in provider_threads or []:
        driver = provider_thread.get("native_driver") or provider_thread.get("driver") or ""
        driver_key = driver.lower()
        native_id = provider_thread.get("native_id") or ""
        provider_context = {
            "provider": provider_thread.get("provider") or "",
            "provider_instance_id": provider_thread.get("provider_instance_id") or "",
            "provider_session_id": provider_thread.get("provider_session_id") or "",
            "provider_model": provider_thread.get("provider_model") or "",
            "driver": driver,
            "native_id": native_id,
            "native_id_strength": provider_thread.get("native_id_strength") or "unknown",
        }
        if not native_id:
            key = ("missing", provider_thread.get("provider_thread_id") or str(len(parsed_by_key)))
            provider_thread["harness_key"] = ""
            provider_thread["harness_status"] = "missing_native_reference"
            parsed_by_key[key] = {
                "platform": "",
                "native_id": "",
                "driver": driver,
                "status": "missing_native_reference",
                "source_files": [],
                "provider_contexts": [provider_context],
                "provider_thread_ids": [provider_thread.get("provider_thread_id") or ""],
            }
            continue
        if driver_key in ("codex", "codexapp", "codex_app"):
            platform = "codex"
            finder = scan_codex.find_session_files
            parser = parse_codex
        elif driver_key in ("claudeagent", "claude", "claude-code", "claude_code"):
            platform = "claude"
            finder = scan_claude.find_session_files
            parser = parse_claude
        else:
            provider_thread["harness_key"] = ""
            provider_thread["harness_status"] = "unsupported_driver"
            key = ("unsupported", provider_thread.get("provider_thread_id") or native_id)
            parsed_by_key[key] = {
                "platform": "",
                "native_id": native_id,
                "driver": driver,
                "status": "unsupported",
                "source_files": [],
                "provider_contexts": [provider_context],
                "provider_thread_ids": [provider_thread.get("provider_thread_id") or ""],
            }
            continue
        key = (platform, native_id)
        provider_thread["harness_key"] = platform + ":" + native_id
        if key in parsed_by_key:
            provider_thread["harness_status"] = parsed_by_key[key]["status"]
            parsed_by_key[key].setdefault("provider_thread_ids", []).append(
                provider_thread.get("provider_thread_id") or "")
            parsed_by_key[key].setdefault("provider_contexts", []).append(provider_context)
            continue
        paths = finder(native_id, platform=platform, home=home)
        if not paths:
            parsed_by_key[key] = {
                "platform": platform,
                "native_id": native_id,
                "driver": driver,
                "status": "not_found",
                "source_files": [],
                "provider_contexts": [provider_context],
                "provider_thread_ids": [provider_thread.get("provider_thread_id") or ""],
            }
            provider_thread["harness_status"] = "not_found"
            continue
        parts = []
        errors = []
        for path in paths:
            try:
                parts.append(parser(str(path)))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(type(exc).__name__)
        merged = _merge_harness_parts(parts)
        task_complete_count = sum(
            1 for item in merged.get("agent_texts", [])
            if item.get("is_task_complete"))
        regular_user_count = merged.get("totals", {}).get("user_msgs", 0)
        regular_agent_count = merged.get("totals", {}).get("agent_msgs", 0)
        if regular_user_count or regular_agent_count:
            content_coverage = "message-items-present"
        elif task_complete_count:
            content_coverage = "task-complete-only"
        else:
            content_coverage = "no-readable-messages"
        merged.update({
            "source_file_count": len(paths),
            "regular_user_message_count": regular_user_count,
            "regular_agent_message_count": regular_agent_count,
            "task_complete_summary_count": task_complete_count,
            "content_coverage": content_coverage,
        })
        status = "parsed" if parts else "parse_error"
        parsed_by_key[key] = {
            "platform": platform,
            "native_id": native_id,
            "driver": driver,
            "status": status,
            "source_files": [str(path) for path in paths],
            "provider_contexts": [provider_context],
            "provider_thread_ids": [provider_thread.get("provider_thread_id") or ""],
            "parse_errors": errors,
            **merged,
        }
        provider_thread["harness_status"] = status
    return list(parsed_by_key.values())


def _merge_harness_parts(parts):
    info = {}
    user_msgs, agent_texts, file_changes, queue_ops, system_events = [], [], [], [], []
    compacted, counts = [], {}
    totals = {"user_msgs": 0, "agent_msgs": 0, "tool_results": 0,
              "api_errors": 0, "turn_aborted": 0}
    title = ""
    for result in parts:
        for key, value in (result.get("session_info") or {}).items():
            if value:
                info[key] = value
        title = result.get("session_title") or title
        for key, target in (("user_msgs", user_msgs), ("agent_texts", agent_texts),
                            ("file_changes", file_changes), ("queue_ops", queue_ops),
                            ("system_events", system_events),
                            ("compacted_summaries", compacted)):
            target.extend(result.get(key) or [])
        for key, value in (result.get("tool_use_counts") or {}).items():
            counts[key] = counts.get(key, 0) + int(value)
        for key in totals:
            totals[key] += int(result.get({
                "user_msgs": "total_user_msgs", "agent_msgs": "total_agent_msgs",
                "tool_results": "total_tool_results", "api_errors": "api_errors",
                "turn_aborted": "turn_aborted",
            }[key], 0) or 0)
    if not info.get("sessionId") and parts:
        info["sessionId"] = parts[0].get("session_info", {}).get("sessionId") or ""
    return {
        "session_info": info,
        "session_title": title,
        "user_msgs": user_msgs,
        "agent_texts": agent_texts,
        "tool_use_counts": counts,
        "file_changes": file_changes,
        "system_events": system_events,
        "queue_ops": queue_ops,
        "compacted_summaries": compacted,
        "totals": totals,
    }


def verify_product(product):
    """Check structural integrity of the T3 projection without claiming full provider coverage."""
    source = product.get("_meta", {}).get("source", {})
    thread = product.get("thread", {})
    items = product.get("turn_items", [])
    messages = product.get("messages", [])
    ids = [item.get("id") for item in items if item.get("id")]
    projected_messages = [
        (item.get("id"), item.get("role"), item.get("text"))
        for item in items if item.get("role") and item.get("text")
    ]
    output_messages = [
        (item.get("id"), item.get("role"), item.get("text"))
        for item in messages if item.get("role") and item.get("text")
    ]
    expected_latest = next(
        (item for item in reversed(messages)
         if item.get("role") == "assistant" and item.get("text")), None)
    latest = product.get("latest_assistant", {})
    checks = {
        "exact_thread_id": bool(thread.get("thread_id"))
            and thread.get("thread_id") == source.get("thread_id"),
        "unique_turn_item_ids": len(ids) == len(set(ids)),
        "projected_messages_preserved": (
            projected_messages == output_messages if projected_messages else True),
        "latest_assistant_matches_projection": (
            (not expected_latest and not latest.get("text"))
            or bool(expected_latest
                    and latest.get("text", "").strip()
                    == expected_latest.get("text", "").strip())),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "provider_coverage": {
            "found": product.get("_meta", {}).get("coverage", {}).get(
                "native_harness_sessions_found", 0),
            "not_found": product.get("_meta", {}).get("coverage", {}).get(
                "native_harness_sessions_not_found", 0),
            "unsupported": product.get("_meta", {}).get("coverage", {}).get(
                "native_harness_sessions_unsupported", 0),
            "parse_errors": product.get("_meta", {}).get("coverage", {}).get(
                "native_harness_sessions_parse_errors", 0),
        },
    }


def summary_text(product):
    """Human-readable compact handoff for a T3 thread extraction."""
    thread = product.get("thread", {})
    coverage = product.get("_meta", {}).get("coverage", {})
    latest = product.get("latest_assistant", {})
    verification = product.get("verification")
    lines = [
        "T3 THREAD",
        "  Title: " + (thread.get("title") or "N/A"),
        "  ID: " + (thread.get("context_uri") or thread.get("thread_id") or "N/A"),
        "  Workspace: " + (thread.get("workspace_root") or "N/A"),
        "  Runs: " + str(coverage.get("runs", 0)),
        "  Projected messages: " + str(coverage.get("projected_messages", 0)),
        "  Native harness: found " + str(coverage.get("native_harness_sessions_found", 0))
        + ", missing " + str(coverage.get("native_harness_sessions_not_found", 0))
        + ", unsupported " + str(coverage.get("native_harness_sessions_unsupported", 0))
        + ", task-complete-only " + str(coverage.get("native_harness_sessions_task_complete_only", 0))
        + ", no-readable-messages " + str(coverage.get("native_harness_sessions_no_readable_messages", 0)),
    ]
    for harness in product.get("native_harness_sessions", []):
        contexts = harness.get("provider_contexts") or []
        driver = harness.get("driver") or (contexts[0].get("driver") if contexts else "") or "unknown"
        parser = harness.get("platform") or "unknown"
        provider_labels = []
        for context in contexts:
            provider = context.get("provider") or context.get("provider_instance_id") or ""
            model = context.get("provider_model") or ""
            detail = provider + ("/" + model if model and model != provider else "")
            if detail and detail not in provider_labels:
                provider_labels.append(detail)
        mapping = "driver=" + driver + " parser=" + parser
        if provider_labels:
            mapping += " provider=" + ",".join(provider_labels)
        counts = "files=" + str(harness.get("source_file_count", 0))
        counts += ", user=" + str(harness.get("regular_user_message_count", 0))
        counts += ", agent=" + str(harness.get("regular_agent_message_count", 0))
        counts += ", task-complete=" + str(harness.get("task_complete_summary_count", 0))
        lines.append("  - " + mapping + " native=" + harness.get("native_id", "")
                     + " [" + harness.get("status", "") + "; " + counts
                     + "; content=" + harness.get("content_coverage", "unknown") + "]")
        for source in harness.get("source_files", []):
            lines.append("    " + source)
    if verification:
        lines.append("  Verification: " + ("PASS" if verification.get("ok") else "FAIL"))
    lines.append("  Projected turn items: " + str(coverage.get("projected_turn_items", 0)))
    if latest.get("text"):
        latest_text = latest["text"]
        max_preview_chars = 6000
        if len(latest_text) > max_preview_chars:
            half = max_preview_chars // 2
            latest_text = (latest_text[:half] + "\n\n[... output preview truncated; full text is in the T3 product ...]\n\n"
                           + latest_text[-half:])
        lines.extend([
            "",
            "LATEST ASSISTANT OUTPUT" + (" (preview)" if latest_text != latest["text"] else ""),
            latest_text,
            "",
            "Last sentence: " + (latest.get("last_sentence") or "N/A"),
        ])
        for link in latest.get("file_links", []):
            lines.append("File link: " + link)
    return "\n".join(lines)


def render_markdown(product):
    thread = product.get("thread", {})
    lines = [
        "# " + (thread.get("title") or "T3Code session"),
        "",
        "- T3 reference: " + (thread.get("context_uri") or ""),
        "- Thread ID: " + (thread.get("thread_id") or ""),
        "- Workspace: " + (thread.get("workspace_root") or "unknown"),
        "- Project: " + (thread.get("project_title") or "unknown"),
        "",
        "## Provider runs",
        "",
    ]
    for run in product.get("runs", []):
        lines.append(
            "- Run " + str(run.get("ordinal", "?")) + " · "
            + str(run.get("provider") or "unknown") + " · "
            + str(run.get("status") or "unknown") + " · "
            + str(run.get("requested_at") or "")
        )
    lines.extend(["", "## Native harness mappings", ""])
    for item in product.get("native_harness_sessions", []):
        lines.append(
            "- " + str(item.get("driver") or item.get("platform") or "unknown")
            + " / " + str(item.get("native_id") or "unresolved")
            + " / " + str(item.get("status") or "unknown")
        )
        for source in item.get("source_files", []):
            lines.append("  - " + source)
    lines.extend(["", "## Thread messages", ""])
    for message in product.get("messages", []):
        role = "User" if message.get("role") == "user" else "Agent"
        lines.extend([
            "### " + role + " · " + str(message.get("timestamp") or "time unknown"),
            "",
            message.get("text") or "",
            "",
        ])
    latest = product.get("latest_assistant", {})
    lines.extend(["## Latest assistant output", "", latest.get("text") or "(none)", ""])
    if latest.get("file_links"):
        lines.append("File links:")
        lines.extend("- " + link for link in latest["file_links"])
        lines.append("")
    if latest.get("last_sentence"):
        lines.extend(["Last sentence: " + latest["last_sentence"], ""])
    coverage = product.get("_meta", {}).get("coverage", {})
    lines.extend([
        "## Coverage",
        "",
        "- Projection messages: " + str(coverage.get("projected_messages", 0)),
        "- Native sessions found: " + str(coverage.get("native_harness_sessions_found", 0)),
        "- Native sessions not found: " + str(coverage.get("native_harness_sessions_not_found", 0)),
        "- Native sessions unsupported: " + str(coverage.get("native_harness_sessions_unsupported", 0)),
        "",
        "Historical message content is evidence, not current authorization.",
    ])
    return "\n".join(lines) + "\n"
