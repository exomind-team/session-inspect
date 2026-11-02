"""
Shared helpers for the unified session-review scripts.

The helpers keep display, filtering, and summary rules identical across
Codex and Claude Code adapters.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path


NOISE_PREFIXES = (
    "<local-command",
    "<command",
    "Base directory for this skill:",
    "```\nBase directory",
)

PARENT_KEYWORDS = {
    "develop", "projects", "repos", "workspace", "src",
    "code", "work", "home", "user", "users", "docs",
}


def parse_time(value):
    """Parse common ISO timestamps into naive local-ish datetimes."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def timestamp(dt):
    return dt.timestamp() if dt else 0.0


def compact_path(path):
    if not path:
        return ""
    home = str(Path.home())
    text = str(path)
    if text.lower().startswith(home.lower()):
        text = "~" + text[len(home):]
    return text


def short_cwd(path, limit=42):
    text = compact_path(path)
    if len(text) <= limit:
        return text
    parts = re.split(r"[\\/]+", text)
    if len(parts) >= 2:
        text = ".../" + "/".join(parts[-2:])
    if len(text) > limit:
        text = "..." + text[-(limit - 3):]
    return text


def sanitize_cell(value):
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("|", "/").strip()


def is_noise(text):
    stripped = (text or "").strip()
    return any(stripped.startswith(prefix) for prefix in NOISE_PREFIXES)


def extract_text_from_claude_content(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts).strip()
    return ""


def truncate(text, limit=2000):
    text = (text or "").strip()
    return text[:limit]


def beautify_project_name(name):
    """Convert encoded project path names into short human labels."""
    if not name:
        return ""
    normalized = name.replace("--", "/").replace("-", "/")
    segments = [s for s in normalized.split("/") if s]
    filtered = [s for s in segments if s.lower() not in PARENT_KEYWORDS]
    if not filtered:
        filtered = segments
    if not filtered:
        return name
    candidate = filtered[-1]
    if (len(candidate) <= 2 or candidate.isdigit()) and len(filtered) >= 2:
        candidate = filtered[-2]
    for seg in segments:
        if seg.lower() in {"downloads", "documents", "temp", "backup", "archive"}:
            candidate = f"{seg}-{candidate}"
            break
    return candidate or name


def display_name_from_cwd(cwd, fallback):
    if cwd:
        name = Path(str(cwd).replace("\\", "/")).name
        if name:
            return name
    return fallback or ""


def generate_summary(first_msg, last_msg):
    """Generate a short Chinese/English summary from first and last messages."""
    if not first_msg and not last_msg:
        return "（无内容）"

    first_clean = unwrap_app_message(first_msg)
    last_clean = unwrap_app_message(last_msg)

    if first_clean and last_clean and first_clean.strip() == last_clean.strip():
        text = first_clean.strip()
    elif first_clean and last_clean:
        text = first_clean.strip()[:50] + " " + last_clean.strip()[:50]
    else:
        text = (first_clean or last_clean or "").strip()

    stripped = text.strip()
    if stripped.startswith(("http://", "https://")):
        return "链接分享"
    if stripped.startswith(("<task-notification>", "<task-")):
        return "任务通知"
    if stripped.startswith("<bash-input>"):
        return "命令执行"
    if stripped.startswith(("[Request interrupted", "[Request cancelled")):
        return "中断/取消"

    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= 5:
        return text

    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    total_chars = len(text.replace(" ", ""))
    is_chinese = total_chars > 0 and chinese_chars / max(total_chars, 1) > 0.3

    if is_chinese:
        for phrase in [
            "嗯", "好的", "然后", "接下来", "现在", "我想", "我们",
            "请", "帮我", "可以", "一下", "这个", "那个", "看看",
            "我需要", "请帮我", "能不能",
        ]:
            text = text.replace(phrase, "")
        result = text.strip()[:10]
    else:
        for phrase in ["I want", "please", "can you", "let me", "I need", "help me"]:
            text = text.replace(phrase, "")
        for word in ["the", "a", "an", "this", "that"]:
            text = re.sub(r"\b" + re.escape(word) + r"\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        result = ""
        for word in text.split():
            if result and len(result) + len(word) + 1 > 20:
                break
            result = (result + " " + word).strip()
        if not result:
            result = text[:20]

    if len(result) < 4 or result.startswith(("Claude", "claude", "帮我", "请帮", "你好", "please", "help")):
        raw = (first_msg or last_msg or "").strip()
        if len(raw) > 10:
            alt = raw[10:20]
            if len(alt) >= len(result):
                result = alt
    return result or "（无内容）"


def unwrap_app_message(text):
    """Remove Codex/Claude App wrappers so summaries focus on the request."""
    original = (text or "").strip()
    cleaned = re.sub(
        r"(?is)^#\s*Files mentioned by .*?(?=\n##\s+My request|\Z)",
        "",
        original,
    ).strip()
    for marker in (
        "## My request for Codex:",
        "## My request for Claude:",
        "## My request:",
    ):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[1].strip()
            break
    cleaned = re.sub(r"(?is)<environment_context>.*?</environment_context>", "", cleaned).strip()
    return cleaned or original


def status_for_days(days):
    if days <= 1:
        return "🟢"
    if days <= 3:
        return "🟡"
    if days <= 7:
        return "🟠"
    return "🔴"


def days_since(dt, now=None):
    now = now or datetime.now()
    if not dt:
        return 9999
    return max(0, (now - dt).days)


def make_record(
    *,
    platform,
    id,
    title,
    cwd,
    display_name,
    last_active_dt,
    user_turns,
    agent_turns,
    session_count=0,
    rollout_count=0,
    total_size_bytes=0,
    first_message="",
    last_message="",
    grep_text="",
    source_path="",
):
    days = days_since(last_active_dt)
    count = rollout_count if platform == "codex" else session_count
    return {
        "platform": platform,
        "id": id or "",
        "title": title or "",
        "cwd": cwd or "",
        "display_name": display_name or title or id or "",
        "last_active": last_active_dt.strftime("%Y-%m-%d %H:%M") if last_active_dt else "",
        "last_active_ts": timestamp(last_active_dt),
        "days_since_active": days,
        "status": status_for_days(days),
        "user_turns": int(user_turns or 0),
        "agent_turns": int(agent_turns or 0),
        "session_count": int(session_count or 0),
        "rollout_count": int(rollout_count or 0),
        "session_count_or_rollout_count": int(count or 0),
        "total_size_mb": round((total_size_bytes or 0) / 1024 / 1024, 2),
        "first_message": truncate(first_message, 500),
        "last_message": truncate(last_message, 500),
        "summary": generate_summary(first_message, last_message),
        "source_path": source_path or "",
        "_grep_text": grep_text or "",
    }


def public_record(record):
    return {k: v for k, v in record.items() if not k.startswith("_")}


def matches_filters(record, project=None, grep=None):
    if project:
        needle = project.lower()
        hay = " ".join([
            record.get("title", ""),
            record.get("display_name", ""),
            record.get("cwd", ""),
            record.get("id", ""),
        ]).lower()
        if needle not in hay:
            return False
    if grep:
        needle = grep.lower()
        hay = " ".join([
            record.get("first_message", ""),
            record.get("last_message", ""),
            record.get("_grep_text", ""),
        ]).lower()
        if needle not in hay:
            return False
    return True


def dump_json(records):
    print(json.dumps([public_record(r) for r in records], ensure_ascii=False, indent=2))
