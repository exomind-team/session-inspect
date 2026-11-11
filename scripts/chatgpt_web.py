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
ATTACHMENT_DOWNLOAD_TIME_BUDGET_SECONDS: float = 20.0
'''Maximum best-effort time spent fetching files after the transcript is saved.'''
ATTACHMENT_PER_FILE_TIME_BUDGET_SECONDS: float = 7.0
'''Maximum time spent on one attachment control and its response.'''
ATTACHMENT_DOWNLOAD_MAX_BYTES: int = 20 * 1024 * 1024
'''Maximum attachment bytes buffered in memory for one file.'''
ATTACHMENT_BUTTON_WAIT_ROUNDS: int = 10
'''Number of short waits for a hover-revealed download control.'''
ATTACHMENT_BUTTON_WAIT_STEP_MS: int = 100
'''Delay between checks for a hover-revealed download control.'''
DOWNLOAD_CAPTURE_SETTLE_MS: int = 400
'''Wait for the clicked download handler to publish its response.'''
DOWNLOAD_LINK_FETCH_TIMEOUT_MS: int = 2500
'''Limit a same-page fetch of an observed download URL.'''
ATTACHMENT_PAGE_URL_TIMEOUT_SECONDS: float = 3.0
'''Maximum wait when checking whether the attachment action navigated away.'''
ATTACHMENT_RESTORE_TIMEOUT_SECONDS: int = 5
'''Maximum wait for one attachment-triggered return navigation.'''
ATTACHMENT_RESTORE_READY_TIMEOUT_SECONDS: int = 8
'''Maximum wait for the conversation DOM after restoring its page.'''
ATTACHMENT_NAVIGATION_POLL_ATTEMPTS: int = 8
'''Number of short checks for a download URL navigation.'''
ATTACHMENT_NAVIGATION_POLL_INTERVAL_SECONDS: float = 0.25
'''Delay between checks for a download URL navigation.'''
ATTACHMENT_CDP_CALL_TIMEOUT_SECONDS: float = 5.0
'''Maximum time for one CDP-backed attachment operation.'''
ATTACHMENT_SHORT_INFO_TIMEOUT_SECONDS: float = 1.0
'''Short page-state read timeout inside the attachment budget.'''
ATTACHMENT_REBIND_TIMEOUT_SECONDS: float = 2.0
'''Maximum time to wait for a hover-revealed attachment button.'''


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
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise BrowserError(
            "BROWSER_PROXY_ERROR",
            "browser proxy returned HTTP {}{}".format(
                exc.code, ": " + detail[:1200] if detail else ""),
            retryable=exc.code >= 500,
        ) from exc
    except TimeoutError as exc:
        endpoint = path.partition("?")[0]
        raise BrowserError(
            "BROWSER_TIMEOUT",
            f"browser proxy request timed out after {timeout}s at {endpoint}",
            retryable=True,
        ) from exc
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


def list_contexts(proxy: str = DEFAULT_PROXY) -> Dict[str, list]:
    """Group open tabs by browser context."""
    raw = _request("/targets", proxy=proxy, timeout=20)
    targets = json.loads(raw.decode("utf-8-sig"))
    contexts: Dict[str, list] = {}
    for target in targets:
        if target.get("type") != "page":
            continue
        context = target.get("browserContextId") or ""
        contexts.setdefault(context, []).append(target)
    return contexts


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


def navigate(target: str, url: str, *, proxy: str = DEFAULT_PROXY,
             timeout_s: int = 60) -> None:
    query = urllib.parse.urlencode({"target": target, "url": url})
    _request("/navigate?" + query, proxy=proxy, timeout=timeout_s)


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
  const seenImage = new Set();

  // Attachments live in resource rows that also contain decorative sibling
  // rows ("文档", "打开文件"). Only rows with a real filename and a download
  // control are treated as files; nothing is claimed as downloaded here.
  const collectFiles = (root) => {
    for (const row of root.querySelectorAll('[class*="resource-row"]')) {
      const firstLine = ((row.innerText || '').split('\n') || [])
        .map(line => line.trim()).filter(Boolean)[0] || '';
      const btn = [...row.querySelectorAll('button')]
        .find(b => /下载|download/i.test(b.getAttribute('aria-label') || ''));
      if (!firstLine || !/\.[A-Za-z0-9]{1,8}$/.test(firstLine)) continue;
      const key = firstLine + '|' + (btn ? '1' : '0');
      let item = attachments.find(a => a.name === firstLine);
      if (!item) {
        item = {name: firstLine, downloadable: !!btn,
                bytes_available: false};
        attachments.push(item);
      } else {
        item.downloadable = item.downloadable || !!btn;
      }
      seenAttachment.add(key);
    }
    for (const img of root.querySelectorAll('img')) {
      const src = img.currentSrc || img.src || '';
      if (!src || /faviconV2|cdn\.auth0\.com/.test(src)) continue;
      if (seenImage.has(src)) continue;
      seenImage.add(src);
      images.push({url: src, alt: img.alt || '',
                   width: img.naturalWidth, height: img.naturalHeight});
    }
    return {
      attachments: [...root.querySelectorAll('[class*="resource-row"]')]
        .map(row => ((row.innerText || '').split('\n') || [])
          .map(line => line.trim()).filter(Boolean)[0] || '')
        .filter(name => attachments.some(a => a.name === name))
        .map(name => ({name: name})),
      images: [...root.querySelectorAll('img')]
        .map(img => img.currentSrc || img.src || '')
        .filter(src => src && images.some(i => i.url === src))
        .map(src => ({url: src}))
    };
  };

  const turns = [...document.querySelectorAll('[data-turn-key]')];
  for (const turn of turns) {
    const units = [...turn.querySelectorAll('[data-content-search-unit-key]')];
    const blocks = units.length
      ? units
      : [...turn.querySelectorAll('div')].filter(
          el => (el.className || '').toString().indexOf('block-') !== -1);
    for (const block of blocks) {
      const unitKey = block.getAttribute('data-content-search-unit-key')
        || block.getAttribute('data-chatgpt-search-unit-key') || '';
      const unitRole = unitKey.match(/:(user|assistant|system|developer|tool)(?::|$)/i);
      const heading = block.querySelector('[data-conversation-role]')
        || block.closest('[data-conversation-role]');
      const role = unitRole ? unitRole[1].toLowerCase()
        : (heading ? heading.getAttribute('data-conversation-role') : 'unknown');
      const md = block.querySelector('[class*="MarkdownRoot"]');
      const text = md ? (md.innerText || '') : (block.innerText || '');
      const resources = collectFiles(block);
      if (!text.trim() && !resources.attachments.length && !resources.images.length) continue;
      const idHolder = block.closest('[data-chatgpt-selection-message-id], [data-chatgpt-search-message-ids]')
        || block.querySelector('[data-chatgpt-selection-message-id]')
        || block.querySelector('[data-chatgpt-search-message-ids]');
      const messageId = idHolder
        ? (idHolder.getAttribute('data-chatgpt-selection-message-id')
           || (idHolder.getAttribute('data-chatgpt-search-message-ids') || '').split(' ')[0])
        : '';
      messages.push({role: role, text: text, id: messageId,
                     turn: turn.getAttribute('data-turn-key') || '',
                     attachments: resources.attachments,
                     images: resources.images});
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
JSON.stringify((() => {
  const turns = [...document.querySelectorAll('[data-turn-key]')];
  const units = [...document.querySelectorAll('[data-content-search-unit-key]')];
  const transcriptTextLen = units.reduce(
    (total, unit) => total + (unit.innerText || '').trim().length, 0);
  const resources = document.querySelectorAll(
    '[class*="resource-row"], [data-turn-key] img').length;
  return {
  turns: turns.length,
  roles: document.querySelectorAll('[data-conversation-role]').length,
  contentUnits: units.length,
  transcriptTextLen: transcriptTextLen,
  resources: resources,
  md: document.querySelectorAll('[class*="MarkdownRoot"]').length,
  textLen: (document.body.innerText || '').length,
  url: location.href,
  denied: /无权访问|没有权限|don't have access|do not have access|You do not have access/i
          .test(document.body.innerText || '')
  };
})())
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
                    "the selected browser context cannot open this conversation; "
                    "use a context with access, or use a share link")
            has_message_structure = state.get("turns", 0) > 0 and (
                state.get("roles", 0) > 0 or state.get("contentUnits", 0) > 0)
            has_rendered_payload = (
                state.get("transcriptTextLen", 0) > 0 or
                state.get("resources", 0) > 0 or
                state.get("md", 0) > 0)
            if has_message_structure and has_rendered_payload:
                return state
        time.sleep(1)
    return last


def _capture_target(target: str, url: str, *, proxy: str,
                    max_scroll_rounds: int,
                    download_attachments: bool) -> Dict[str, Any]:
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
        snapshot = fetch_attachments(snapshot, url, proxy=proxy)
    return snapshot


def fetch_attachments(snapshot: Dict[str, Any], url: str, *,
                      proxy: str = DEFAULT_PROXY,
                      time_budget_s: float = ATTACHMENT_DOWNLOAD_TIME_BUDGET_SECONDS
                      ) -> Dict[str, Any]:
    """Attempt listed file downloads within one bounded time budget."""
    target = snapshot.get("target")
    if not target:
        return snapshot
    deadline = time.monotonic() + max(0.0, time_budget_s)
    fetched = []
    attachments = snapshot.get("attachments") or []
    for index, item in enumerate(attachments):
        if not item.get("downloadable"):
            fetched.append(item)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fetched.append({
                "name": item.get("name"), "status": "not-attempted",
                "bytes_available": False,
                "note": "attachment download time budget exhausted",
            })
            continue
        try:
            result = fetch_attachment(
                target, item["name"], proxy=proxy,
                time_budget_s=min(remaining, ATTACHMENT_PER_FILE_TIME_BUDGET_SECONDS))
        except BrowserError as exc:
            result = {"name": item.get("name"), "status": "failed",
                      "bytes_available": False, "note": str(exc)}
        fetched.append(result)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for pending in attachments[index + 1:]:
                fetched.append({
                    "name": pending.get("name"), "status": "not-attempted",
                    "bytes_available": False,
                    "note": "attachment download time budget exhausted",
                })
            break
        current_url = _page_url(
            target, proxy=proxy,
            timeout_s=min(ATTACHMENT_PAGE_URL_TIMEOUT_SECONDS, remaining))
        if current_url and current_url != url:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                for pending in attachments[index + 1:]:
                    fetched.append({
                        "name": pending.get("name"), "status": "not-attempted",
                        "bytes_available": False,
                        "note": "attachment tab navigated and time budget was exhausted",
                    })
                break
            try:
                navigate(target, url, proxy=proxy,
                         timeout_s=max(1, min(ATTACHMENT_RESTORE_TIMEOUT_SECONDS,
                                              int(remaining))))
                wait_for_content(target, proxy=proxy,
                                 timeout_s=max(1, min(ATTACHMENT_RESTORE_READY_TIMEOUT_SECONDS,
                                                      int(remaining))))
            except BrowserError:
                for pending in attachments[index + 1:]:
                    fetched.append({
                        "name": pending.get("name"), "status": "not-attempted",
                        "bytes_available": False,
                        "note": "could not restore conversation after attachment navigation",
                    })
                break
    snapshot["attachments"] = fetched
    snapshot["attachment_download"] = {
        "budget_seconds": time_budget_s,
        "elapsed_seconds": round(time.monotonic() - (deadline - max(0.0, time_budget_s)), 3),
    }
    return snapshot


OPEN_IN_CONTEXT_SCRIPT = r"""
(() => {
  const old = document.getElementById('__si_context_open');
  if (old) old.remove();
  const a = document.createElement('a');
  a.id = '__si_context_open';
  a.href = %s;
  a.target = '_blank';
  a.textContent = '';
  a.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;'
    + 'z-index:2147483646;background:transparent;';
  document.documentElement.appendChild(a);
  return JSON.stringify({ok: true});
})()
"""

REMOVE_CONTEXT_ANCHOR = r"""
(() => {
  const a = document.getElementById('__si_context_open');
  if (a) a.remove();
  return 'ok';
})()
"""


def _host_for_context(targets) -> str:
    """Pick a normal web page in the requested context to open the new tab."""
    for target in targets:
        if (target.get("url") or "") == "about:blank":
            return target["targetId"]
    for target in targets:
        url = target.get("url") or ""
        if url.startswith(("https://", "http://")):
            return target["targetId"]
    for target in targets:
        url = target.get("url") or ""
        if not url.startswith(("chrome-extension://", "edge://", "chrome://",
                              "devtools://")):
            return target["targetId"]
    return ""


def open_in_context(context_id: str, url: str, *, proxy: str = DEFAULT_PROXY,
                    targets=None, timeout_s: int = 15) -> str:
    """Open a new tab inside an existing browser context.

    ``Target.createTarget`` is not exposed by the proxy, so a real click on a
    synthetic ``target=_blank`` link is used: it carries a user gesture and
    therefore lands in the same browser context as the host tab.
    """
    if targets is None:
        targets = list_contexts(proxy).get(context_id, [])
    if not targets:
        raise BrowserError("BROWSER_CONTEXT_NOT_FOUND",
                           "browser context has no open page")
    host = _host_for_context(targets)
    if not host:
        raise BrowserError("NO_CONTEXT_TAB",
                           "browser context has no page to open from")
    before = {target.get("targetId") for target in targets}
    script = OPEN_IN_CONTEXT_SCRIPT % json.dumps(url, ensure_ascii=False)
    evaluate(host, script, proxy=proxy, timeout=30)
    try:
        click_raw = _request("/clickAt?target=" + urllib.parse.quote(host),
                             proxy=proxy, data=b"#__si_context_open", timeout=30)
        click_result = json.loads(click_raw.decode("utf-8", errors="replace"))
        if click_result.get("clicked") is False and not click_result.get("sent"):
            raise BrowserError("CONTEXT_TAB_CLICK_FAILED",
                               click_result.get("error") or
                               "could not open a tab in the selected browser context",
                               retryable=True)

        deadline = time.time() + timeout_s
        new_target = None
        while time.time() < deadline:
            time.sleep(0.25)
            for target in list_contexts(proxy).get(context_id, []):
                if target.get("targetId") in before:
                    continue
                if target.get("browserContextId") != context_id:
                    continue
                new_target = target
                break
            if new_target:
                break
        if not new_target:
            raise BrowserError("CONTEXT_TAB_NOT_OPENED",
                               "clicking a link did not create a tab in the selected "
                               "browser context",
                               retryable=True)

        target_id = new_target["targetId"]
        expected_path = urllib.parse.urlsplit(url).path.rstrip("/")
        # Keep the temporary anchor in place until Chromium has committed the
        # target=_blank navigation. Removing it immediately can leave a blank
        # tab and force an unnecessary slow Page.navigate fallback.
        deadline = time.time() + min(timeout_s, 8)
        committed = False
        while time.time() < deadline:
            current = next((item for item in list_contexts(proxy).get(context_id, [])
                            if item.get("targetId") == target_id), None)
            current_url = (current or {}).get("url") or ""
            if (current_url == url or
                    urllib.parse.urlsplit(current_url).path.rstrip("/") == expected_path):
                committed = True
                break
            time.sleep(0.25)
        if not committed:
            navigate(target_id, url, proxy=proxy)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            current = next((item for item in list_contexts(proxy).get(context_id, [])
                            if item.get("targetId") == target_id), None)
            current_url = (current or {}).get("url") or ""
            if (current_url == url or
                    urllib.parse.urlsplit(current_url).path.rstrip("/") == expected_path):
                return target_id
            time.sleep(0.25)
        close_tab(target_id, proxy=proxy)
        raise BrowserError("CONTEXT_NAVIGATION_FAILED",
                           "the new tab did not reach the ChatGPT conversation URL",
                           retryable=True)
    finally:
        try:
            evaluate(host, REMOVE_CONTEXT_ANCHOR, proxy=proxy, timeout=10)
        except BrowserError:
            pass


def capture_conversation(url: str, *, proxy: str = DEFAULT_PROXY,
                         keep_tab: bool = False,
                         max_scroll_rounds: int = MAX_SCROLL_ROUNDS,
                         download_attachments: bool = False,
                         browser_context_id: Optional[str] = None) -> Dict[str, Any]:
    """Open a conversation and return a structural snapshot.

    ``browser_context_id`` selects one existing browser context for navigation.
    The identifier is used only for routing: it is omitted from the returned
    snapshot and diagnostics. Access failures never fall through to another
    browser context.
    """
    if not is_supported_url(url):
        raise BrowserError("UNSUPPORTED_URL",
                           "only https://chatgpt.com/ conversation or share URLs are accepted")
    if browser_context_id is None:
        target = open_tab(url, proxy=proxy)
        captured = False
        try:
            snapshot = _capture_target(
                target, url, proxy=proxy,
                max_scroll_rounds=max_scroll_rounds,
                download_attachments=download_attachments)
            captured = True
            return snapshot
        finally:
            if not keep_tab or not captured:
                close_tab(target, proxy=proxy)

    contexts = list_contexts(proxy)
    targets = contexts.get(browser_context_id)
    if targets is None:
        raise BrowserError("BROWSER_CONTEXT_NOT_FOUND",
                           "selected browser context is not present in the running browser")
    target = open_in_context(browser_context_id, url, proxy=proxy,
                             targets=targets)
    captured = False
    try:
        snapshot = _capture_target(
            target, url, proxy=proxy,
            max_scroll_rounds=max_scroll_rounds,
            download_attachments=download_attachments)
        captured = True
        return snapshot
    finally:
        if not keep_tab or not captured:
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


INSTALL_DOWNLOAD_CAPTURE_SCRIPT = r"""
(() => {
  if (window.__siDownloadCapture) {
    window.__siDownloadCapture.links.length = 0;
    window.__siDownloadCapture.blobs.length = 0;
    return 'ready';
  }
  const state = window.__siDownloadCapture = {links: [], blobs: []};
  const rememberLink = (href, download) => {
    if (href) state.links.push({href: String(href), download: String(download || '')});
  };
  const rememberNode = (node) => {
    if (!node) return;
    if (node.nodeType === 1 && node.tagName === 'A')
      rememberLink(node.href, node.download);
    if (node.querySelectorAll)
      for (const a of node.querySelectorAll('a')) rememberLink(a.href, a.download);
  };
  const originalClick = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function() {
    rememberLink(this.href, this.download);
    return originalClick.apply(this, arguments);
  };
  const originalOpen = window.open;
  window.open = function(href) {
    rememberLink(href, '');
    return originalOpen.apply(this, arguments);
  };
  const originalCreate = URL.createObjectURL;
  URL.createObjectURL = function(value) {
    const href = originalCreate.call(this, value);
    if (value instanceof Blob) state.blobs.push({href, blob: value, type: value.type || ''});
    return href;
  };
  const originalFetch = window.fetch;
  window.fetch = async function() {
    const response = await originalFetch.apply(this, arguments);
    const input = arguments[0];
    const href = typeof input === 'string' ? input : (input && input.url) || '';
    const type = response.headers.get('content-type') || '';
    const disposition = response.headers.get('content-disposition') || '';
    if (/file|download|usercontent|attachment/i.test(href) ||
        /attachment/i.test(disposition)) {
      try {
        const blob = await response.clone().blob();
        state.blobs.push({href, blob, type, status: response.status});
      } catch {}
    }
    return response;
  };
  new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) rememberNode(node);
  }).observe(document.documentElement, {childList: true, subtree: true});
  return 'ready';
})()
"""

READ_DOWNLOAD_CAPTURE_SCRIPT = r"""
(async (maxBytes, settleMs, linkTimeoutMs) => {
  await new Promise(resolve => setTimeout(resolve, settleMs));
  const state = window.__siDownloadCapture || {links: [], blobs: []};
  const encode = async (blob) => {
    if (!blob || blob.size > maxBytes) return {status: 'too-large', bytes: blob?.size || 0};
    const type = (blob.type || '').toLowerCase();
    if (/application\/(json|problem\+json)|text\/html/.test(type))
      return {status: 'not-file', bytes: blob.size, type};
    const prefix = await blob.slice(0, 512).text().catch(() => '');
    if (/^\s*\{\s*"(?:detail|error|message)"\s*:/.test(prefix))
      return {status: 'not-file', bytes: blob.size, type};
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000)
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return {status: 'fetched', bytes: bytes.length, type: blob.type || '', base64: btoa(binary)};
  };
  for (const item of state.blobs) {
    if (item.blob && (!item.status || item.status === 200)) {
      const result = await encode(item.blob);
      if (result.status === 'fetched' || result.status === 'too-large')
        return JSON.stringify(result);
    }
  }
  for (const item of state.links.slice().reverse().slice(0, 1)) {
    try {
      const hint = new URL(item.href, location.href);
      if (!item.download && !/file|download|usercontent|attachment/i.test(hint.pathname)) continue;
      const sameOrigin = hint.origin === location.origin;
      const signedFileHost = /(^|\.)oaiusercontent\.com$/i.test(hint.hostname);
      if (!sameOrigin && !signedFileHost) continue;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), linkTimeoutMs);
      let response;
      try {
        response = await fetch(item.href, {
          credentials: sameOrigin ? 'include' : 'omit', signal: controller.signal
        });
      } finally {
        clearTimeout(timer);
      }
      const length = Number(response.headers.get('content-length') || 0);
      if (length > maxBytes) continue;
      if (!response.ok || /application\/(json|problem\+json)|text\/html/i.test(
          response.headers.get('content-type') || '')) continue;
      const result = await encode(await response.blob());
      if (result.status === 'fetched' || result.status === 'too-large')
        return JSON.stringify(result);
    } catch {}
  }
  const hints = state.links.map(item => {
    try {
      const u = new URL(item.href, location.href);
      return {protocol: u.protocol, host: u.host, path: u.pathname, download: !!item.download};
    } catch { return {protocol: '', host: '', path: '', download: !!item.download}; }
  });
  return JSON.stringify({status: 'no-captured-bytes', links: hints,
    blobs: state.blobs.map(item => ({type: item.type || '', status: item.status || null,
      bytes: item.blob?.size || 0}))});
})(%d, %d, %d)
"""


def _remaining_timeout(
    deadline: float,
    maximum: float = ATTACHMENT_CDP_CALL_TIMEOUT_SECONDS,
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BrowserError("ATTACHMENT_TIME_BUDGET_EXHAUSTED",
                           "attachment download time budget exhausted")
    return min(maximum, remaining)


def fetch_attachment(target: str, name: str, *, proxy: str = DEFAULT_PROXY,
                     max_bytes: int = ATTACHMENT_DOWNLOAD_MAX_BYTES,
                     time_budget_s: float = ATTACHMENT_PER_FILE_TIME_BUDGET_SECONDS
                     ) -> Dict[str, Any]:
    """Download one attachment through the page's own download control.

    In current ChatGPT builds the control navigates this tab to a short-lived
    signed URL. The bytes must be read immediately after that navigation: a
    stale signature returns 403. Nothing is written to disk and no credential
    is exported. Oversized payloads are refused rather than truncated.
    """
    script = """
    (() => {
      const name = %s;
      const rows = [...document.querySelectorAll('[class*="resource-row"]')];
      const row = rows.find(r => {
        const first = ((r.innerText || '').split('\\n') || [])
          .map(line => line.trim()).filter(Boolean)[0] || '';
        return first === name;
      });
      if (!row) return JSON.stringify({status: 'not-found'});
      const btn = [...row.querySelectorAll('button')]
        .find(b => /下载|download/i.test(b.getAttribute('aria-label') || ''));
      if (!btn) return JSON.stringify({status: 'no-download-button'});
      row.id = '__si_attachment_row';
      btn.id = '__si_attachment_download';
      return JSON.stringify({status: 'ready'});
    })()
    """ % json.dumps(name, ensure_ascii=False)
    deadline = time.monotonic() + max(0.0, time_budget_s)
    before_url = _page_url(
        target, proxy=proxy,
        timeout_s=_remaining_timeout(
            deadline, maximum=ATTACHMENT_SHORT_INFO_TIMEOUT_SECONDS))
    try:
        evaluate(target, INSTALL_DOWNLOAD_CAPTURE_SCRIPT, proxy=proxy,
                 timeout=_remaining_timeout(deadline))
    except BrowserError:
        pass
    try:
        prepared = evaluate(target, script, proxy=proxy,
                            timeout=_remaining_timeout(deadline))
    except BrowserError as exc:
        return {"name": name, "status": "prepare-failed",
                "bytes_available": False, "note": str(exc)}
    if not isinstance(prepared, dict) or prepared.get("status") != "ready":
        return {"name": name, "status": (prepared or {}).get("status", "prepare-failed"),
                "bytes_available": False}
    try:
        hover_raw = _request("/hover?target=" + urllib.parse.quote(target),
                             proxy=proxy, data=b"#__si_attachment_row",
                             timeout=_remaining_timeout(deadline))
        hover = json.loads(hover_raw.decode("utf-8", errors="replace"))
        # ChatGPT may replace the resource row when hover reveals its toolbar.
        # Re-find the button after the hover transition instead of clicking a
        # stale node that may have been detached or still have a zero-size box.
        time.sleep(0.25)
        rebind_script = r"""
        (async () => {
          const name = %s;
          for (let i = 0; i < 10; i++) {
            const row = [...document.querySelectorAll('[class*="resource-row"]')].find(r => {
              const first = ((r.innerText || '').split('\n') || [])
                .map(line => line.trim()).filter(Boolean)[0] || '';
              return first === name;
            });
            const btn = row && [...row.querySelectorAll('button')]
              .find(b => /下载|download/i.test(b.getAttribute('aria-label') || ''));
            if (row && btn) {
              row.id = '__si_attachment_row';
              btn.id = '__si_attachment_download';
              const rect = btn.getBoundingClientRect();
              const style = getComputedStyle(btn);
              if (rect.width > 0 && rect.height > 0 &&
                  style.display !== 'none' && style.visibility !== 'hidden' &&
                  style.pointerEvents !== 'none' && rect.left >= 0 && rect.top >= 0 &&
                  rect.right <= innerWidth && rect.bottom <= innerHeight)
                return JSON.stringify({status: 'ready'});
            }
            await new Promise(resolve => setTimeout(resolve, 100));
          }
          return JSON.stringify({status: 'not-visible'});
        })()
         """ % json.dumps(name, ensure_ascii=False)
        rebound = evaluate(target, rebind_script, proxy=proxy,
                           timeout=_remaining_timeout(
                               deadline, maximum=ATTACHMENT_REBIND_TIMEOUT_SECONDS))
        if not isinstance(rebound, dict) or rebound.get("status") != "ready":
            return {"name": name, "status": "click-failed",
                    "bytes_available": False,
                    "hovered": hover.get("hovered"),
                    "note": "the download button did not become visible after hovering"}
        click_raw = _request("/clickAt?target=" + urllib.parse.quote(target),
                             proxy=proxy, data=b"#__si_attachment_download",
                             timeout=_remaining_timeout(deadline))
        click = json.loads(click_raw.decode("utf-8", errors="replace"))
    except (BrowserError, json.JSONDecodeError) as exc:
        hover = locals().get("hover", {})
        click = {"clicked": False, "sent": False, "error": str(exc)}
    try:
        captured = evaluate(
            target, READ_DOWNLOAD_CAPTURE_SCRIPT % (
                max_bytes, DOWNLOAD_CAPTURE_SETTLE_MS,
                DOWNLOAD_LINK_FETCH_TIMEOUT_MS),
                            proxy=proxy, timeout=_remaining_timeout(deadline))
    except BrowserError:
        captured = {}
    if isinstance(captured, dict) and captured.get("status") == "fetched":
        result = {
            "name": name, "status": "fetched", "http_status": click.get("status"),
            "content_type": captured.get("type") or "",
            "bytes": int(captured.get("bytes") or 0),
            "bytes_available": True,
            "sha256": hashlib.sha256(base64.b64decode(captured["base64"])).hexdigest(),
            "content_base64": captured["base64"],
        }
        return result
    if isinstance(captured, dict) and captured.get("status") == "too-large":
        return {"name": name, "status": "too-large",
                "bytes": int(captured.get("bytes") or 0),
                "bytes_available": False,
                "note": "attachment exceeds the configured size limit"}
    if not click.get("sent") and _page_url(
            target, proxy=proxy,
            timeout_s=_remaining_timeout(
                deadline, maximum=ATTACHMENT_SHORT_INFO_TIMEOUT_SECONDS)) == before_url:
        return {"name": name, "status": "click-failed",
                "bytes_available": False,
                "hover_sent": hover.get("sent"),
                "hovered": hover.get("hovered"),
                "click_observed": click.get("clicked"),
                "download_capture": captured,
                "note": click.get("error") or
                "the page did not receive the download click"}
    signed_url = _wait_for_download_url(
        target, proxy=proxy, before_url=before_url,
        attempts=max(1, min(
            ATTACHMENT_NAVIGATION_POLL_ATTEMPTS,
            int(max(0.0, deadline - time.monotonic()) /
                (ATTACHMENT_NAVIGATION_POLL_INTERVAL_SECONDS +
                 ATTACHMENT_SHORT_INFO_TIMEOUT_SECONDS)))),
        timeout_s=_remaining_timeout(
            deadline, maximum=ATTACHMENT_SHORT_INFO_TIMEOUT_SECONDS))
    if not signed_url:
        return {"name": name,
                "status": "no-navigation" if click.get("sent") else "click-failed",
                "bytes_available": False,
                "hover_sent": hover.get("sent"),
                "hovered": hover.get("hovered"),
                "click_sent": click.get("sent"),
                "click_observed": click.get("clicked"),
                "download_capture": captured,
                "note": (click.get("error") or
                         ("click event was not observed" if not click.get("clicked") else
                          "the download control did not navigate to a file view"))}
    try:
        payload = evaluate(target, ATTACHMENT_BYTES_SCRIPT % max_bytes,
                           proxy=proxy, timeout=_remaining_timeout(deadline))
    except BrowserError as exc:
        return {"name": name, "status": "failed", "bytes_available": False,
                "note": str(exc)}
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


def _page_url(target: str, *, proxy: str, timeout_s: float = 10) -> str:
    try:
        info = json.loads(_request(
            "/info?target=" + urllib.parse.quote(target),
            proxy=proxy, timeout=timeout_s))
        return info.get("url") or ""
    except (BrowserError, json.JSONDecodeError):
        return ""


def _wait_for_download_url(target: str, *, proxy: str,
                           before_url: str = "",
                           attempts: int = ATTACHMENT_NAVIGATION_POLL_ATTEMPTS,
                           timeout_s: float = ATTACHMENT_SHORT_INFO_TIMEOUT_SECONDS
                           ) -> str:
    for _ in range(attempts):
        time.sleep(ATTACHMENT_NAVIGATION_POLL_INTERVAL_SECONDS)
        url = _page_url(target, proxy=proxy, timeout_s=timeout_s)
        if not url or url == before_url:
            continue
        if "oaiusercontent" in url or "/files/" in url:
            return url
    return ""
