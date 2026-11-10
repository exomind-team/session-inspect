"""ChatGPT web conversation normalization.

Two input shapes are supported:

1. ``browser_snapshot``: a bounded, structured snapshot collected from a
   controllable browser (see ``chatgpt_web.py``). This is the only path that
   can reach a private/workspace conversation, because it runs inside the
   user's authenticated browser session.
2. ``decoded_share``: an already decoded share payload (the offline prototype
   in session-inspect's iteration pack). Everything here is structural; no
   HTML decoding, no script execution, no network access.

The normalizer never invents identity, attachments or completeness. Anything
it cannot prove is reported as a gap.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


SCHEMA = "chatgpt-conversation/v1"
ADAPTER_VERSION = "chatgpt-core/1"


class ChatGPTError(ValueError):
    """A structural problem with the supplied ChatGPT conversation data."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text_from_content_blocks(content: Any) -> str:
    """Read only readable text blocks; unknown block types are counted, not guessed."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in ("text", "input_text", "output_text"):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _normalize_role(role: str) -> str:
    role = (role or "").lower()
    if role in ("user", "human"):
        return "user"
    if role in ("assistant", "ai", "chatgpt"):
        return "assistant"
    if role in ("system", "developer", "tool"):
        return role
    return "unknown"


def normalize_browser_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a snapshot collected from a controllable browser tab."""
    if not isinstance(snapshot, dict):
        raise ChatGPTError("INVALID_SNAPSHOT", "snapshot must be an object")
    raw_messages = snapshot.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ChatGPTError(
            "EMPTY_CONVERSATION",
            "the page yielded no readable messages; refusing to report an empty success")

    messages: List[Dict[str, Any]] = []
    unknown_role_count = 0
    attachments_by_name = {
        item.get("name"): item for item in (snapshot.get("attachments") or [])
        if isinstance(item, dict) and item.get("name")
    }
    images_by_url = {
        item.get("url"): item for item in (snapshot.get("images") or [])
        if isinstance(item, dict) and item.get("url")
    }
    for index, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            text = ""
        role = _normalize_role(item.get("role", ""))
        if role == "unknown":
            unknown_role_count += 1
        block_id = item.get("turn") or item.get("id") or "message-{}".format(index)
        message_attachments = []
        for ref in item.get("attachments") or []:
            name = ref.get("name") if isinstance(ref, dict) else str(ref)
            message_attachments.append(
                attachments_by_name.get(name)
                or (ref if isinstance(ref, dict) else {"name": name}))
        message_images = []
        for ref in item.get("images") or []:
            url = ref.get("url") if isinstance(ref, dict) else str(ref)
            message_images.append(
                images_by_url.get(url)
                or (ref if isinstance(ref, dict) else {"url": url}))
        messages.append({
            "index": index,
            "native_id": str(block_id),
            "role": role,
            "text": text,
            "timestamp": item.get("timestamp") or "",
            "source": "browser-dom",
            "attachments": message_attachments,
            "images": message_images,
        })

    if not any(message["text"].strip() for message in messages):
        raise ChatGPTError(
            "EMPTY_TEXT",
            "no message carried text; HTML shell or authentication page is not a conversation")

    attachments = snapshot.get("attachments") or []
    images = snapshot.get("images") or []
    coverage = {
        "source": "browser-dom",
        "message_count": len(messages),
        "plain_text_only": True,
        "unknown_role_count": unknown_role_count,
        "attachments_listed": len(attachments),
        "attachments_fetched": sum(
            1 for item in attachments if item.get("bytes_available")),
        "images_listed": len(images),
        "images_fetched": sum(1 for item in images if item.get("bytes_available")),
        "complete": False,
        "scope_note": (
            "DOM snapshot of the rendered conversation. It reflects what the "
            "page rendered in this session, not the workspace's stored record; "
            "reasoning, tool payloads and unrendered branches are out of scope."),
    }
    return {
        "schema": SCHEMA,
        "adapter_version": ADAPTER_VERSION,
        "platform": "chatgpt-web",
        "source": {
            "kind": "browser-snapshot",
            "url": snapshot.get("url") or "",
            "title": snapshot.get("title") or "",
            "captured_at": snapshot.get("captured_at") or "",
        },
        "messages": messages,
        "attachments": attachments,
        "images": images,
        "coverage": coverage,
        "snapshot_sha256": _canonical_sha256(messages),
    }


def _linear_conversation(payload: Any) -> List[Dict[str, Any]]:
    """Return the explicit linear view or a uniquely determined parent chain."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ChatGPTError("INVALID_SHARE", "share payload must be an object or array")
    for key in ("linear_conversation", "linearConversation", "messages"):
        linear = payload.get(key)
        if isinstance(linear, list):
            return linear
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        raise ChatGPTError("UNKNOWN_SHARE_SHAPE",
                           "payload has neither a linear view nor a mapping tree")
    return _chain_from_mapping(payload, mapping)


def _chain_from_mapping(payload: Dict[str, Any], mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    parent: Dict[str, str] = {}
    children: Dict[str, List[str]] = {}
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        node_parent = node.get("parent")
        if node_parent:
            parent[node_id] = str(node_parent)
            children.setdefault(str(node_parent), []).append(node_id)
    if not mapping:
        raise ChatGPTError("EMPTY_SHARE", "mapping tree is empty")

    current = payload.get("current_node")
    if current and str(current) in mapping:
        order: List[str] = []
        seen = set()
        cursor = str(current)
        while cursor in mapping and cursor not in seen:
            seen.add(cursor)
            order.append(cursor)
            cursor = parent.get(cursor, "")
        if len(seen) != len(order):
            raise ChatGPTError("CYCLE", "parent chain contains a cycle")
        return [mapping[node_id] for node_id in reversed(order)]

    leaves = [node_id for node_id in mapping if node_id not in children]
    leaves = [node_id for node_id in leaves if mapping[node_id].get("message")]
    if len(leaves) != 1:
        raise ChatGPTError(
            "AMBIGUOUS_BRANCH",
            "found {} candidate leaf nodes and no current_node; refusing to "
            "flatten a branch".format(len(leaves)))
    order = []
    seen = set()
    cursor = leaves[0]
    while cursor in mapping and cursor not in seen:
        seen.add(cursor)
        order.append(cursor)
        cursor = parent.get(cursor, "")
    if cursor and cursor in mapping:
        raise ChatGPTError("CYCLE", "parent chain contains a cycle")
    return [mapping[node_id] for node_id in reversed(order)]


def normalize_decoded_share(payload: Any, *, share_url: str = "") -> Dict[str, Any]:
    """Normalize an already decoded share payload. No network or HTML decoding."""
    items = _linear_conversation(payload)
    messages: List[Dict[str, Any]] = []
    unsupported_parts = 0
    skipped_without_text = 0
    duplicate_ids = 0
    seen_ids = set()

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        message = item.get("message") if isinstance(item.get("message"), dict) else item
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        role = _normalize_role(author.get("role") or message.get("role") or "")
        content = message.get("content") or {}
        if isinstance(content, dict):
            text = _text_from_content_blocks(content.get("parts"))
            if content.get("content_type") == "text" and isinstance(content.get("text"), str):
                text = text or content["text"]
        else:
            text = ""
        if not text.strip():
            skipped_without_text += 1
        native_id = str(item.get("id") or message.get("id") or "node-{}".format(index))
        if native_id in seen_ids:
            duplicate_ids += 1
        seen_ids.add(native_id)
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        for key in ("attachments", "files", "images"):
            if metadata.get(key):
                unsupported_parts += len(metadata.get(key) or [])
        messages.append({
            "index": index,
            "native_id": native_id,
            "role": role,
            "text": text,
            "timestamp": message.get("create_time") or "",
            "source": "decoded-share",
            "attachments": [],
        })

    if not messages:
        raise ChatGPTError("EMPTY_SHARE", "no messages could be normalized")
    if not any(message["text"].strip() for message in messages):
        raise ChatGPTError("EMPTY_TEXT", "share payload carried no readable text")

    return {
        "schema": SCHEMA,
        "adapter_version": ADAPTER_VERSION,
        "platform": "chatgpt-share",
        "source": {
            "kind": "decoded-share",
            "url": share_url,
            "title": (payload.get("title") if isinstance(payload, dict) else "") or "",
        },
        "messages": messages,
        "attachments": [],
        "images": [],
        "coverage": {
            "source": "decoded-share",
            "message_count": len(messages),
            "skipped_without_text": skipped_without_text,
            "unsupported_parts": unsupported_parts,
            "duplicate_native_ids": duplicate_ids,
            "attachment_bytes": "not-in-share-payload",
            "complete": False,
            "scope_note": (
                "Selected share view only. Attachments and images are not "
                "fetched by this path."),
        },
        "snapshot_sha256": _canonical_sha256(messages),
    }


def build_product(normalized: Dict[str, Any], *, title: str = "",
                  reason: str = "", include_base64: bool = False) -> Dict[str, Any]:
    """Present a normalized conversation in the rescued-session/v1 envelope."""
    attachments = []
    for item in normalized.get("attachments") or []:
        record = {key: value for key, value in item.items()
                  if key != "content_base64"}
        if include_base64 and item.get("content_base64"):
            record["content_base64"] = item["content_base64"]
        attachments.append(record)
    transcript = []
    for index, message in enumerate(normalized.get("messages") or []):
        transcript.append({
            "node_id": message.get("native_id") or "n{}".format(index),
            "type": "REQUEST" if message.get("role") == "user" else "RESPONSE",
            "content": message.get("text") or "",
            "timestamp": message.get("timestamp") or "",
            "role": message.get("role") or "",
            "source": message.get("source") or "",
        })
    source = normalized.get("source") or {}
    coverage = normalized.get("coverage") or {}
    return {
        "_meta": {
            "schema": "rescued-session/v1",
            "platform": normalized.get("platform"),
            "adapter_version": normalized.get("adapter_version"),
            "source": {
                "kind": source.get("kind"),
                "url": source.get("url") or "",
                "title": source.get("title") or "",
                "captured_at": source.get("captured_at") or "",
                "note": reason or "",
            },
            "conversation_count": 1,
            "coverage": coverage,
            "snapshot_sha256": normalized.get("snapshot_sha256"),
            "verification": {
                "ok": bool(transcript) and bool(coverage.get("message_count")),
                "scope": (
                    "normalized messages match the declared source view; "
                    "does not prove the source conversation is complete"),
                "checks": {
                    "messages_present": bool(transcript),
                    "message_count_matches": len(transcript) == int(
                        coverage.get("message_count") or 0),
                    "no_empty_text": all(
                        (item.get("content") or "").strip() or item.get("role") != "user"
                        for item in transcript),
                },
            },
        },
        "conversations": [{
            "id": "",
            "title": title or source.get("title") or "",
            "inserted_at": (transcript[0]["timestamp"] if transcript else ""),
            "updated_at": (transcript[-1]["timestamp"] if transcript else ""),
            "rescue_reason": reason or None,
            "stats": {
                "message_count": len(transcript),
                "user_messages": sum(
                    1 for item in transcript if item["type"] == "REQUEST"),
                "assistant_messages": sum(
                    1 for item in transcript if item["type"] == "RESPONSE"),
            },
            "attachments": attachments,
            "images": normalized.get("images") or [],
            "transcript": transcript,
            "raw_mapping": None,
        }],
    }
