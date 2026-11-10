"""Bounded, read-only collection from a controllable browser tab.

The collector runs against the web-access CDP proxy and is intentionally
conservative:
- it only reads the page, never sends a chat message or mutates a conversation;
- it never exports credentials, session tokens or cookies;
- it records what it could not fetch instead of pretending completeness.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
import hashlib
from typing import Any, Dict, Optional


DEFAULT_PROXY = "http://localhost:3456"
MAX_SCROLL_ROUNDS = 60
SCROLL_SETTLE_MS = 700


class BrowserError(RuntimeError):
    """The browser surface could not be driven or yielded nothing usable."""

    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _request(path: str, *, proxy: str, data: Optional[bytes] = None,
             timeout: int = 60) -> bytes:
    url = proxy.rstrip("/") + path
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise BrowserError(
            "BROWSER_UNAVAILABLE",
            "cannot reach the browser proxy at {}: {}".format(proxy, exc),
            retryable=True,
        ) from exc


def proxy_ready(proxy: str = DEFAULT_PROXY) -> bool:
    try:
        _request("/targets", proxy=proxy, timeout=5)
        return True
    except BrowserError:
        return False


def open_tab(url: str, *, proxy: str = DEFAULT_PROXY) -> str:
    query = urllib.parse.urlencode({"url": url})
    payload = json.loads(_request("/new?" + query, proxy=proxy, timeout=90))
    target = payload.get("targetId") or ""
    if not target:
        raise BrowserError("TAB_CREATE_FAILED", "proxy did not return a target id")
    return target


def close_tab(target: str, *, proxy: str = DEFAULT_PROXY) -> None:
    try:
        _request("/close?target=" + urllib.parse.quote(target),
                 proxy=proxy, timeout=20)
    except BrowserError:
        pass


def navigate(target: str, url: str, *, proxy: str = DEFAULT_PROXY) -> None:
    query = urllib.parse.urlencode({"target": target, "url": url})
    _request("/navigate?" + query, proxy=proxy, timeout=60)


def evaluate(target: str, script: str, *, proxy: str = DEFAULT_PROXY,
             timeout: int = 90) -> Any:
    raw = _request("/eval?target=" + urllib.parse.quote(target),
                   proxy=proxy, data=script.encode("utf-8"), timeout=timeout)
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if "error" in payload:
        raise BrowserError("EVAL_FAILED", str(payload["error"]),
                           retryable="timeout" in str(payload["error"]).lower())
    value = payload.get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _scroll_to_load(target: str, *, proxy: str,
                    max_rounds: int = MAX_SCROLL_ROUNDS) -> Dict[str, Any]:
    """Scroll the virtualised transcript until new turns stop appearing."""
    script = """
    (async () => {
      // Background tabs throttle rAF and scroll; make sure the transcript is
      // laid out even when the tab is not visible, then scroll its container.
      const candidates = ['.thread-scroll-container',
        '[class*="scrollContainer"]',
        '[data-scrollable]',
        'main'];
      let sc = null;
      for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (el && el.scrollHeight > el.clientHeight + 20) { sc = el; break; }
      }
      if (!sc) {
        sc = [...document.querySelectorAll('*')].find(
          el => el.scrollHeight > el.clientHeight + 200
                && getComputedStyle(el).overflowY !== 'visible');
      }
      if (!sc) return JSON.stringify({status:'no-scroll-container'});
      const previousVisibility = document.visibilityState;
      const seen = new Set();
      let rounds = 0, stable = 0, previous = -1;
      while (rounds < %d && stable < 3) {
        rounds++;
        for (const el of document.querySelectorAll('[data-turn-key]')) {
          seen.add(el.getAttribute('data-turn-key'));
        }
        const before = sc.scrollTop;
        sc.scrollBy(0, Math.max(600, Math.floor(sc.clientHeight * 1.5)));
        await new Promise(r => setTimeout(r, %d));
        if (sc.scrollTop === before && sc.scrollHeight === previous) stable++;
        else stable = 0;
        previous = sc.scrollHeight;
        if (sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 2) {
          break;
        }
      }
      return JSON.stringify({
        status: 'ok', rounds: rounds, turns: seen.size,
        scrollHeight: sc.scrollHeight, scrollTop: sc.scrollTop,
        visibility: previousVisibility
      });
    })()
    """ % (max_rounds, SCROLL_SETTLE_MS)
    return evaluate(target, script, proxy=proxy, timeout=240)


EXTRACT_SCRIPT = r"""
(() => {
  const messages = [];
  const attachments = [];
  const images = [];
  const seenAttachment = new Set();

  // Attachments live in resource rows that also contain decorative sibling
  // rows ("文档", "打开文件"). Only rows with a real filename and a download
  // control are treated as files; nothing is claimed as downloaded here.
  const collectFiles = (root) => {
    for (const row of root.querySelectorAll('[class*="resource-row"]')) {
      const firstLine = ((row.innerText || '').split('\n') || [])
        .map(line => line.trim()).filter(Boolean)[0] || '';
      const btn = [...row.querySelectorAll('button')]
        .find(b => (b.getAttribute('aria-label') || '').indexOf('下载') !== -1);
      if (!firstLine || !/\.[A-Za-z0-9]{1,8}$/.test(firstLine)) continue;
      const key = firstLine + '|' + (btn ? '1' : '0');
      if (seenAttachment.has(key)) continue;
      seenAttachment.add(key);
      attachments.push({name: firstLine, downloadable: !!btn,
                        bytes_available: false});
    }
    for (const img of root.querySelectorAll('img')) {
      const src = img.currentSrc || img.src || '';
      if (!src || /faviconV2|cdn\.auth0\.com/.test(src)) continue;
      images.push({url: src, alt: img.alt || '',
                   width: img.naturalWidth, height: img.naturalHeight});
    }
  };

  const turns = [...document.querySelectorAll('[data-turn-key]')];
  for (const turn of turns) {
    const units = [...turn.querySelectorAll('[data-content-search-unit-key]')];
    const blocks = units.length
      ? units
      : [...turn.querySelectorAll('div')].filter(
          el => (el.className || '').toString().indexOf('block-') !== -1);
    for (const block of blocks) {
      const heading = block.querySelector('[data-conversation-role]');
      const role = heading
        ? heading.getAttribute('data-conversation-role') : 'unknown';
      const md = block.querySelector('[class*="MarkdownRoot"]');
      const text = md ? (md.innerText || '') : '';
      if (!text.trim()) continue;
      const idHolder = block.querySelector('[data-chatgpt-selection-message-id]')
        || block.querySelector('[data-chatgpt-search-message-ids]');
      const messageId = idHolder
        ? (idHolder.getAttribute('data-chatgpt-selection-message-id')
           || (idHolder.getAttribute('data-chatgpt-search-message-ids') || '').split(' ')[0])
        : '';
      messages.push({role: role, text: text, id: messageId,
                     turn: turn.getAttribute('data-turn-key') || ''});
      collectFiles(block);
    }
    if (!blocks.length) collectFiles(turn);
  }
  return JSON.stringify({
    url: location.href,
    title: document.title,
    messages: messages,
    attachments: attachments,
    images: images
  });
})()
"""


READY_SCRIPT = r"""
JSON.stringify({
  turns: document.querySelectorAll('[data-turn-key]').length,
  roles: document.querySelectorAll('[data-conversation-role]').length,
  md: document.querySelectorAll('[class*="MarkdownRoot"]').length,
  textLen: (document.body.innerText || '').length,
  url: location.href,
  denied: /无权访问|没有权限|don't have access|do not have access|You do not have access/i
          .test(document.body.innerText || '')
})
"""


def wait_for_content(target: str, *, proxy: str = DEFAULT_PROXY,
                     timeout_s: int = 45) -> Dict[str, Any]:
    """Wait until the transcript is actually rendered.

    The share shell renders before its content arrives, so a fixed sleep would
    sometimes read an empty page and report a false failure.
    """
    deadline = time.time() + timeout_s
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        try:
            state = evaluate(target, READY_SCRIPT, proxy=proxy, timeout=30)
        except BrowserError as exc:
            if exc.code == "ACCESS_DENIED":
                raise
            state = {}
        if isinstance(state, dict):
            last = state
            if state.get("denied"):
                raise BrowserError(
                    "ACCESS_DENIED",
                    "the signed-in account cannot open this conversation; "
                    "confirm the account and workspace, or use a share link")
            if state.get("md") and state.get("textLen", 0) > 400:
                return state
        time.sleep(1)
    return last


def capture_conversation(url: str, *, proxy: str = DEFAULT_PROXY,
                         keep_tab: bool = False,
                         max_scroll_rounds: int = MAX_SCROLL_ROUNDS,
                         download_attachments: bool = False) -> Dict[str, Any]:
    """Open a conversation tab, load it fully and return a structural snapshot."""
    if not is_supported_url(url):
        raise BrowserError("UNSUPPORTED_URL",
                           "only https://chatgpt.com/ conversation or share URLs are accepted")
    target = open_tab(url, proxy=proxy)
    try:
        readiness = wait_for_content(target, proxy=proxy)
        if not readiness.get("md"):
            raise BrowserError(
                "CONTENT_NOT_RENDERED",
                "no rendered transcript found; the page may require sign-in "
                "or may not be a conversation view",
                retryable=True)
        scroll = _scroll_to_load(target, proxy=proxy, max_rounds=max_scroll_rounds)
        snapshot = evaluate(target, EXTRACT_SCRIPT, proxy=proxy, timeout=120)
        if not isinstance(snapshot, dict):
            raise BrowserError("SNAPSHOT_FAILED", "page extraction returned no object")
        snapshot["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        snapshot["scroll"] = scroll
        snapshot["target"] = target
        if download_attachments and snapshot.get("attachments"):
            fetched = []
            for item in snapshot["attachments"]:
                if not item.get("downloadable"):
                    fetched.append(item)
                    continue
                fetched.append(fetch_attachment(target, item["name"], proxy=proxy))
                # Re-open the conversation: the download navigated this tab away.
                navigate(target, url, proxy=proxy)
                wait_for_content(target, proxy=proxy, timeout_s=30)
            snapshot["attachments"] = fetched
        return snapshot
    finally:
        if not keep_tab:
            close_tab(target, proxy=proxy)


def is_supported_url(url: str) -> bool:
    return url.startswith("https://chatgpt.com/")


def is_share_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path
    return path.startswith("/s/") or path.startswith("/share/")


ATTACHMENT_BYTES_SCRIPT = r"""
(async () => {
  try {
    const r = await fetch(location.href);
    const b = await r.arrayBuffer();
    const bytes = new Uint8Array(b);
    if (bytes.length > %d) {
      return JSON.stringify({status: 'too-large', bytes: bytes.length});
    }
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return JSON.stringify({
      status: r.status,
      contentType: r.headers.get('content-type') || '',
      bytes: bytes.length,
      base64: btoa(binary)
    });
  } catch (error) {
    return JSON.stringify({status: 0, error: String(error)});
  }
})()
"""


def fetch_attachment(target: str, name: str, *, proxy: str = DEFAULT_PROXY,
                     max_bytes: int = 20 * 1024 * 1024) -> Dict[str, Any]:
    """Download one attachment through the page's own download control.

    In current ChatGPT builds the control navigates this tab to a short-lived
    signed URL. The bytes must be read immediately after that navigation: a
    stale signature returns 403. Nothing is written to disk and no credential
    is exported. Oversized payloads are refused rather than truncated.
    """
    script = """
    (async () => {
      const name = %s;
      const rows = [...document.querySelectorAll('[class*="resource-row"]')];
      const row = rows.find(r => {
        const first = ((r.innerText || '').split('\\n') || [])
          .map(line => line.trim()).filter(Boolean)[0] || '';
        return first === name;
      });
      if (!row) return JSON.stringify({status: 'not-found'});
      const btn = [...row.querySelectorAll('button')]
        .find(b => (b.getAttribute('aria-label') || '').indexOf('下载') !== -1);
      if (!btn) return JSON.stringify({status: 'no-download-button'});
      // Current builds only reveal the control after the row is hovered.
      for (const type of ['mouseover', 'mousemove', 'mousedown', 'mouseup']) {
        btn.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true}));
      }
      btn.click();
      return JSON.stringify({status: 'clicked'});
    })()
    """ % json.dumps(name, ensure_ascii=False)
    before_url = _page_url(target, proxy=proxy)
    try:
        evaluate(target, script, proxy=proxy, timeout=30)
    except BrowserError:
        # The click can tear down the JS context as navigation starts; that is
        # expected and the download may still succeed.
        pass
    signed_url = _wait_for_download_url(
        target, proxy=proxy, before_url=before_url)
    if not signed_url:
        return {"name": name, "status": "no-navigation",
                "bytes_available": False,
                "note": "the download control did not navigate to a file view"}
    payload = evaluate(target, ATTACHMENT_BYTES_SCRIPT % max_bytes,
                       proxy=proxy, timeout=120)
    result = {
        "name": name,
        "status": "fetched" if isinstance(payload, dict)
        and payload.get("status") == 200 else "failed",
        "http_status": (payload or {}).get("status") if isinstance(payload, dict) else 0,
        "content_type": (payload or {}).get("contentType") or "",
        "bytes": int((payload or {}).get("bytes") or 0),
        "bytes_available": False,
        "signed_url_host": urllib.parse.urlsplit(signed_url).netloc,
    }
    if isinstance(payload, dict) and payload.get("base64"):
        raw = base64.b64decode(payload["base64"])
        result["bytes_available"] = True
        result["bytes"] = len(raw)
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        result["content_base64"] = payload["base64"]
    elif isinstance(payload, dict) and payload.get("status") == "too-large":
        result["note"] = "attachment exceeds the configured size limit"
    else:
        result["note"] = "attachment bytes were not served to the page"
    return result


def _page_url(target: str, *, proxy: str) -> str:
    try:
        info = json.loads(_request(
            "/info?target=" + urllib.parse.quote(target),
            proxy=proxy, timeout=10))
        return info.get("url") or ""
    except (BrowserError, json.JSONDecodeError):
        return ""


def _wait_for_download_url(target: str, *, proxy: str,
                           before_url: str = "", attempts: int = 60) -> str:
    for _ in range(attempts):
        time.sleep(0.25)
        url = _page_url(target, proxy=proxy)
        if not url or url == before_url:
            continue
        if "oaiusercontent" in url or "/files/" in url:
            return url
    return ""
