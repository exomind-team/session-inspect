"""Codex JSONL extractor.

This is the detailed read path. It shares identity, framing, body and coverage
semantics with the overview radar through ``codex_core`` so the two views
cannot disagree about what a Codex session contains.
"""
from __future__ import annotations

from collections import Counter

import codex_core


def empty_result() -> dict:
    return {
        "platform": "codex",
        "session_info": {},
        "user_msgs": [],
        "agent_texts": [],
        "tool_uses": [],
        "tool_use_counts": Counter(),
        "file_changes": [],
        "file_change_set": set(),
        "system_events": [],
        "queue_ops": [],
        "session_title": "",
        "total_user_msgs": 0,
        "total_agent_msgs": 0,
        "total_tool_results": 0,
        "api_errors": 0,
        "turn_aborted": 0,
        "compacted_summaries": [],
        "coverage": {},
        "sources": [],
    }


def extract_many(paths):
    """Parse several rollouts of one thread and return a merged result."""
    parsed = [codex_core.parse_source(path) for path in paths]
    readable = [source for source in parsed if source.get("readable")]
    merged = codex_core.merge_sources(readable)
    return _result_from_merged(merged, unreadable=[
        source for source in parsed if not source.get("readable")])


def extract(filepath):
    """Single-file entry point, kept for callers and older tests."""
    source = codex_core.parse_source(filepath)
    merged = codex_core.merge_sources([source] if source.get("readable") else [])
    return _result_from_merged(
        merged, unreadable=[] if source.get("readable") else [source])


def _result_from_merged(merged, unreadable=None):
    result = empty_result()
    unreadable = unreadable or []
    sources = merged.get("sources") or []
    if not sources:
        coverage = dict(merged.get("coverage") or {})
        coverage["compressed_sources"] = [
            item for source in unreadable
            for item in (source.get("coverage") or {}).get("compressed_sources", [])
        ]
        result["coverage"] = coverage
        result["sources"] = unreadable
        return result

    info = {}
    for source in sources:
        for key, value in (source.get("session_info") or {}).items():
            if value:
                info[key] = value
    if not info.get("sessionId"):
        info["sessionId"] = sources[0].get("session_id") or ""
    result["session_info"] = info
    result["session_title"] = next(
        (source.get("title") for source in sources if source.get("title")), "")

    for event in merged.get("events") or []:
        text = event.get("text") or ""
        timestamp = event.get("timestamp") or ""
        role = event.get("role")
        if role == "user":
            result["total_user_msgs"] += 1
            result["user_msgs"].append({
                "text": text,
                "timestamp": timestamp,
                "native_id": event.get("native_id") or "",
                "channel": event.get("channel") or "",
            })
        elif role == "assistant":
            item = {
                "text": text,
                "timestamp": timestamp,
                "native_id": event.get("native_id") or "",
                "channel": event.get("channel") or "",
            }
            if event.get("is_task_complete"):
                item["is_task_complete"] = True
            else:
                # task_complete is a terminal summary, not a regular agent
                # message; T3 coverage classification depends on this split.
                result["total_agent_msgs"] += 1
            result["agent_texts"].append(item)
        elif role == "system":
            result["system_events"].append({
                "text": text,
                "timestamp": timestamp,
                "channel": event.get("channel") or "",
            })

    result["tool_use_counts"] = Counter(merged.get("tool_uses") or {})
    result["compacted_summaries"] = list(merged.get("compacted_summaries") or [])
    result["turn_aborted"] = int(merged.get("turn_aborted") or 0)
    result["coverage"] = dict(merged.get("coverage") or {})
    result["sources"] = sources
    return result


def build_transcript(result):
    """Linearise a native Codex result into rescued-session transcript items."""
    items = []
    ordinal = 0
    for message in result.get("user_msgs") or []:
        ordinal += 1
        items.append({
            "node_id": "n{}".format(ordinal),
            "type": "REQUEST",
            "content": message.get("text", ""),
            "timestamp": message.get("timestamp", ""),
        })
    for message in result.get("agent_texts") or []:
        ordinal += 1
        items.append({
            "node_id": "n{}".format(ordinal),
            "type": "RESPONSE",
            "content": message.get("text", ""),
            "timestamp": message.get("timestamp", ""),
            "is_task_complete": bool(message.get("is_task_complete")),
        })
    return items


def build_product(result, *, session_id, source_note=""):
    """Build a rescued-session/v1-shaped product for a native Codex session."""
    sources = result.get("sources") or []
    transcript = build_transcript(result)
    coverage = dict(result.get("coverage") or {})
    paths = [source.get("path", "") for source in sources]
    doc = {
        "_meta": {
            "schema": "rescued-session/v1",
            "platform": "codex",
            "source": {
                "kind": "native-codex-rollout",
                "session_id": session_id,
                "paths": paths,
                "files": [
                    {
                        "name": source.get("path", "").replace("\\", "/").split("/")[-1],
                        "sha256": source.get("sha256", ""),
                        "bytes": source.get("bytes", 0),
                        "compressed": bool(source.get("compressed")),
                        "decoder": source.get("decoder", ""),
                    }
                    for source in sources
                ],
                "note": source_note,
            },
            "conversation_count": 1,
            "coverage": coverage,
            "verification": {},
            "sources": [
                {
                    "kind": "native-codex-rollout",
                    "file": source.get("path", "").replace("\\", "/").split("/")[-1],
                    "sha256": source.get("sha256", ""),
                    "event_count": len(source.get("events") or []),
                }
                for source in sources
            ],
        },
        "conversations": [{
            "id": session_id,
            "title": result.get("session_title") or session_id,
            "inserted_at": (transcript[0]["timestamp"] if transcript else ""),
            "updated_at": (transcript[-1]["timestamp"] if transcript else ""),
            "rescue_reason": source_note or None,
            "stats": {
                "user_message_count": result.get("total_user_msgs", 0),
                "agent_message_count": result.get("total_agent_msgs", 0),
                "turn_aborted": result.get("turn_aborted", 0),
                "source_count": len(sources),
            },
            "attachments": [],
            "transcript": transcript,
            "raw_mapping": None,
        }],
    }
    doc["_meta"]["verification"] = codex_core.verify_sources(
        session_id=session_id,
        sources=sources,
        merged={
            "events": [
                {"role": "user" if item["type"] == "REQUEST" else "assistant",
                 "is_task_complete": item["type"] == "TASK_COMPLETE"}
                for item in transcript
            ],
        },
        product=doc,
    )
    return doc
