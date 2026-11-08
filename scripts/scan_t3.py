"""T3Code thread scanner for the unified session-inspect overview."""
from __future__ import annotations

import json
import t3code
from scan_common import (
    compact_path,
    make_record,
    matches_filters,
    parse_time,
    short_cwd,
)


def scan(days=7, project=None, grep=None, session_id=None, home=None, db_path=None):
    store = t3code.T3Store(db_path=db_path, home=home)
    try:
        rows = _thread_rows(store)
        wanted = None
        if session_id:
            wanted, _ = t3code.normalize_thread_ref(session_id)
        records = []
        for row in rows:
            thread_id = row.get("thread_id") or ""
            if wanted and thread_id != wanted:
                continue
            if row.get("deleted_at"):
                continue
            if not wanted and days > 0:
                active = parse_time(row.get("updated_at"))
                if active:
                    from scan_common import days_since
                    if days_since(active) > days:
                        continue
            if project:
                metadata = " ".join((row.get(k) or "") for k in
                                    ("title", "project_title", "workspace_root", "thread_id"))
                if project.lower() not in metadata.lower():
                    continue
            if grep and not _metadata_matches(row, grep) and not _thread_has_text(store, thread_id, grep):
                continue
            record = _make_record(store, row)
            if not wanted and days > 0 and record["days_since_active"] > days:
                continue
            if matches_filters(record, project=project, grep=grep):
                records.append(record)
        records.sort(key=lambda item: item["last_active_ts"], reverse=True)
        return records
    finally:
        store.close()


def _thread_rows(store):
    if store._has("orchestration_v2_projection_threads"):
        sql = (
            "SELECT t.thread_id,t.project_id,t.title,t.default_provider,t.created_at,"
            "t.updated_at,t.archived_at,t.deleted_at,p.title AS project_title,"
            "p.workspace_root FROM orchestration_v2_projection_threads t "
            "LEFT JOIN projection_projects p ON p.project_id=t.project_id"
        )
        return store._all(sql)
    if store._has("projection_threads"):
        sql = (
            "SELECT t.thread_id,t.project_id,t.title,t.created_at,t.updated_at,"
            "t.archived_at,t.deleted_at,p.title AS project_title,"
            "p.workspace_root FROM projection_threads t "
            "LEFT JOIN projection_projects p ON p.project_id=t.project_id"
        )
        return store._all(sql)
    raise t3code.T3Error("T3 database has no supported thread projection table")


def _message_rows(store, thread_id):
    if store._has("orchestration_v2_projection_turn_items"):
        rows = store._all(
            "SELECT type,ordinal,updated_at,payload_json FROM "
            "orchestration_v2_projection_turn_items WHERE thread_id=? "
            "AND type IN ('user_message','assistant_message') ORDER BY ordinal",
            (thread_id,))
        out = []
        for row in rows:
            payload = t3code._json_object(row.get("payload_json"))
            text = t3code._payload_text(payload)
            role = t3code.MESSAGE_TYPES.get(row.get("type"), "")
            if text and role:
                out.append({
                    "role": role,
                    "text": text,
                    "timestamp": payload.get("completedAt") or row.get("updated_at") or "",
                    "ordinal": row.get("ordinal"),
                })
        if out:
            return out
    if store._has("projection_thread_messages"):
        rows = store._all(
            "SELECT role,text,created_at,updated_at FROM projection_thread_messages "
            "WHERE thread_id=? ORDER BY created_at,message_id", (thread_id,))
        return [{
            "role": row.get("role") or "",
            "text": row.get("text") or "",
            "timestamp": row.get("created_at") or row.get("updated_at") or "",
        } for row in rows if row.get("text")]
    return []


def _metadata_matches(row, needle):
    return needle.lower() in " ".join((row.get(k) or "") for k in
                                      ("title", "project_title", "workspace_root", "thread_id")).lower()


def _thread_has_text(store, thread_id, needle):
    pattern = "%" + needle.lower() + "%"
    if store._has("orchestration_v2_projection_turn_items"):
        row = store.connection.execute(
            "SELECT 1 FROM orchestration_v2_projection_turn_items "
            "WHERE thread_id=? AND lower(payload_json) LIKE ? LIMIT 1",
            (thread_id, pattern)).fetchone()
        if row:
            return True
    if store._has("projection_thread_messages"):
        row = store.connection.execute(
            "SELECT 1 FROM projection_thread_messages "
            "WHERE thread_id=? AND lower(text) LIKE ? LIMIT 1",
            (thread_id, pattern)).fetchone()
        if row:
            return True
    return False


def _provider_chain(store, thread_id):
    if not store._has("orchestration_v2_projection_provider_threads"):
        return []
    rows = store._all(
        "SELECT provider,driver,provider_instance_id,first_run_ordinal,last_run_ordinal "
        "FROM orchestration_v2_projection_provider_threads WHERE thread_id=? "
        "ORDER BY first_run_ordinal", (thread_id,))
    seen = set()
    chain = []
    for row in rows:
        key = (row.get("driver") or "", row.get("provider_instance_id") or row.get("provider") or "")
        if key in seen:
            continue
        seen.add(key)
        chain.append(row.get("driver") or row.get("provider") or "")
    return chain


def _make_record(store, row):
    thread_id = row.get("thread_id") or ""
    messages = _message_rows(store, thread_id)
    users = [message for message in messages if message["role"] == "user"]
    agents = [message for message in messages if message["role"] == "assistant"]
    first_message = users[0]["text"] if users else ""
    last_message = messages[-1]["text"] if messages else ""
    project = row.get("project_title") or ""
    cwd = row.get("workspace_root") or ""
    title = row.get("title") or thread_id
    timestamps = [parse_time(message.get("timestamp")) for message in messages]
    timestamps = [value for value in timestamps if value]
    active = max(timestamps) if timestamps else parse_time(row.get("updated_at"))
    record = make_record(
        platform="t3",
        id=thread_id,
        title=title,
        cwd=cwd,
        display_name=project or short_cwd(cwd) or title,
        last_active_dt=active,
        user_turns=len(users),
        agent_turns=len(agents),
        session_count=1,
        rollout_count=0,
        first_message=first_message,
        last_message=last_message,
        grep_text=" ".join(message["text"] for message in messages),
        source_path=str(store.path),
    )
    record["workspace"] = short_cwd(cwd)
    record["cwd_compact"] = compact_path(cwd)
    record["context_uri"] = t3code.normalize_thread_ref(thread_id)[1]
    record["project_id"] = row.get("project_id") or ""
    record["project_title"] = project
    record["provider_chain"] = _provider_chain(store, thread_id)
    record["run_count"] = len(store._all(
        "SELECT run_id FROM orchestration_v2_projection_runs WHERE thread_id=?",
        (thread_id,))) if store._has("orchestration_v2_projection_runs") else 0
    record["archived"] = bool(row.get("archived_at"))
    record["database_schema"] = "orchestration-v2" if store._has(
        "orchestration_v2_projection_threads") else "legacy-projection"
    return record
