"""CDP-backed browser sandbox (Playwright Chromium).

Host decision: cdp_playwright — see docs/browser-sandbox.md.

All Playwright sync calls run on a dedicated worker thread. FastAPI request
threads must never touch Page/Browser objects directly (greenlet crash → 400).
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..runtime.browser_protocol import (
    PROTOCOL_VERSION,
    STYLE_KEYS,
    DomComponentHint,
    DomElementPayload,
    DomRect,
)
from .tenant_context import get_user_id

HOST_KIND = "cdp_playwright"

_SELECT_BOOTSTRAP = r"""
(() => {
  if (window.__sidekickSelectBooted) return true;
  window.__sidekickSelectBooted = true;

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function xpathFor(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return '//*[@id="' + el.id + '"]';
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && cur !== document.documentElement) {
      let i = 1;
      let sib = cur.previousElementSibling;
      while (sib) {
        if (sib.tagName === cur.tagName) i++;
        sib = sib.previousElementSibling;
      }
      parts.unshift(cur.tagName.toLowerCase() + "[" + i + "]");
      cur = cur.parentElement;
    }
    return "/html/body/" + parts.join("/");
  }

  function cssPath(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + cssEscape(el.id);
    const parts = [];
    let cur = el;
    let depth = 0;
    while (cur && cur.nodeType === 1 && depth < 6) {
      let part = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift("#" + cssEscape(cur.id));
        break;
      }
      if (cur.classList && cur.classList.length) {
        part += "." + Array.from(cur.classList).slice(0, 2).map(cssEscape).join(".");
      }
      parts.unshift(part);
      cur = cur.parentElement;
      depth++;
    }
    return parts.join(" > ");
  }

  function reactHint(el) {
    const key = Object.keys(el).find(
      (k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")
    );
    if (!key) return null;
    let fiber = el[key];
    for (let i = 0; i < 12 && fiber; i++) {
      const t = fiber.type;
      if (typeof t === "function") {
        const name = t.displayName || t.name;
        if (name && !name.startsWith("_")) {
          return { name, framework: "react", file_hint: "" };
        }
      }
      if (typeof t === "object" && t && t.displayName) {
        return { name: String(t.displayName), framework: "react", file_hint: "" };
      }
      fiber = fiber.return;
    }
    return null;
  }

  function attrsOf(el) {
    const out = {};
    if (!el || !el.attributes) return out;
    for (const a of Array.from(el.attributes).slice(0, 24)) {
      if (a.name === "class" || a.name === "style") continue;
      let v = a.value || "";
      if (v.length > 120) v = v.slice(0, 120) + "…";
      out[a.name] = v;
    }
    return out;
  }

  function stylesOf(el) {
    const cs = window.getComputedStyle(el);
    const keys = __STYLE_KEYS__;
    const out = {};
    for (const k of keys) {
      try { out[k] = cs[k]; } catch (e) {}
    }
    return out;
  }

  function payloadFor(el) {
    const r = el.getBoundingClientRect();
    const classes = el.classList ? Array.from(el.classList).slice(0, 12) : [];
    let outer = "";
    try {
      outer = (el.outerHTML || "").slice(0, 4000);
    } catch (e) {}
    return {
      kind: "dom-element",
      protocol_version: __PROTOCOL_VERSION__,
      url: location.href,
      tag: (el.tagName || "").toLowerCase(),
      xpath: xpathFor(el),
      css_path: cssPath(el),
      id: el.id || "",
      classes,
      attributes: attrsOf(el),
      inner_text: (el.innerText || "").trim().slice(0, 800),
      role: el.getAttribute("role") || "",
      rect: { x: r.x, y: r.y, width: r.width, height: r.height },
      computed_styles: stylesOf(el),
      component: reactHint(el),
      outer_html: outer,
      selected_at: Date.now() / 1000,
    };
  }

  let hl = null;
  let armed = false;
  let resolver = null;

  function ensureHl() {
    if (hl) return hl;
    hl = document.createElement("div");
    hl.setAttribute("data-sidekick-select-hl", "1");
    Object.assign(hl.style, {
      position: "fixed",
      pointerEvents: "none",
      zIndex: "2147483646",
      border: "2px solid #2563eb",
      background: "rgba(37,99,235,0.12)",
      borderRadius: "2px",
      display: "none",
    });
    document.documentElement.appendChild(hl);
    return hl;
  }

  function onMove(ev) {
    if (!armed) return;
    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    if (!el || el === hl) return;
    const r = el.getBoundingClientRect();
    const box = ensureHl();
    box.style.display = "block";
    box.style.left = r.x + "px";
    box.style.top = r.y + "px";
    box.style.width = Math.max(0, r.width) + "px";
    box.style.height = Math.max(0, r.height) + "px";
  }

  function onClick(ev) {
    if (!armed) return;
    ev.preventDefault();
    ev.stopPropagation();
    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    if (!el || el === hl) return;
    armed = false;
    if (hl) hl.style.display = "none";
    document.removeEventListener("mousemove", onMove, true);
    document.removeEventListener("click", onClick, true);
    const payload = payloadFor(el);
    const r = resolver;
    resolver = null;
    if (r) r(payload);
  }

  window.__sidekickSelectArm = function (timeoutMs) {
    return new Promise((resolve) => {
      if (armed && resolver) {
        resolver(null);
      }
      armed = true;
      resolver = resolve;
      ensureHl();
      document.addEventListener("mousemove", onMove, true);
      document.addEventListener("click", onClick, true);
      const ms = Math.max(1000, Number(timeoutMs) || 60000);
      setTimeout(() => {
        if (!armed) return;
        armed = false;
        if (hl) hl.style.display = "none";
        document.removeEventListener("mousemove", onMove, true);
        document.removeEventListener("click", onClick, true);
        const r = resolver;
        resolver = null;
        if (r) r(null);
      }, ms);
    });
  };

  window.__sidekickSelectCancel = function () {
    armed = false;
    if (hl) hl.style.display = "none";
    document.removeEventListener("mousemove", onMove, true);
    document.removeEventListener("click", onClick, true);
    const r = resolver;
    resolver = null;
    if (r) r(null);
  };

  return true;
})()
""".replace("__STYLE_KEYS__", json.dumps(list(STYLE_KEYS))).replace(
    "__PROTOCOL_VERSION__", str(PROTOCOL_VERSION)
)


@dataclass
class _Job:
    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None


class _PlaywrightThread:
    """Single thread that owns sync Playwright for all users."""

    def __init__(self) -> None:
        self._q: queue.Queue[Optional[_Job]] = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name="sidekick-playwright", daemon=True
        )
        self._started = False
        self._start_lock = threading.Lock()

    def ensure_started(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._thread.start()
            self._started = True

    def call(self, fn: Callable[[], Any], *, timeout: float = 120.0) -> Any:
        self.ensure_started()
        job = _Job(fn=fn)
        self._q.put(job)
        if not job.done.wait(timeout=timeout):
            raise TimeoutError("browser sandbox worker timed out")
        if job.error is not None:
            raise RuntimeError(str(job.error)) from job.error
        return job.result

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            try:
                job.result = job.fn()
            except BaseException as exc:  # noqa: BLE001
                job.error = exc
            finally:
                job.done.set()


_WORKER = _PlaywrightThread()


class BrowserSandbox:
    """Per-user Chromium session; all CDP ops marshalled onto `_WORKER`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def host_kind(self) -> str:
        return HOST_KIND

    def playwright_available(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, (
                "Playwright not installed. Run: pip install playwright && playwright install chromium "
                f"({exc})"
            )
        return True, ""

    def status(self, user_id: Optional[str] = None) -> dict[str, Any]:
        uid = user_id or get_user_id()
        ok, err = self.playwright_available()
        with self._lock:
            sess = self._sessions.get(uid)
            info = None
            if sess and sess.get("page"):
                info = {
                    "url": sess.get("url") or "about:blank",
                    "started_at": sess.get("started_at"),
                    "ready": True,
                    "host": HOST_KIND,
                }
        return {
            "host": HOST_KIND,
            "available": ok,
            "message": err,
            "session": info,
        }

    def ensure_session(
        self,
        *,
        user_id: Optional[str] = None,
        url: str = "",
        headless: bool = False,
    ) -> dict[str, Any]:
        ok, err = self.playwright_available()
        if not ok:
            raise RuntimeError(err)
        uid = user_id or get_user_id()
        target = (url or "").strip()

        def _op() -> dict[str, Any]:
            with self._lock:
                sess = self._sessions.get(uid)
            if sess and sess.get("page"):
                if target:
                    self._navigate_unlocked(sess, target)
                with self._lock:
                    return self._public(sess)
            return self._ensure_on_worker(uid, target, headless=headless)

        return _WORKER.call(_op, timeout=90.0)

    def close(self, user_id: Optional[str] = None) -> None:
        uid = user_id or get_user_id()

        def _op() -> None:
            with self._lock:
                sess = self._sessions.pop(uid, None)
            if not sess:
                return
            for key in ("context", "browser", "pw"):
                obj = sess.get(key)
                try:
                    if obj is None:
                        continue
                    if key == "pw":
                        obj.stop()
                    else:
                        obj.close()
                except Exception:
                    pass

        _WORKER.call(_op, timeout=30.0)

    def navigate(self, url: str, *, user_id: Optional[str] = None) -> dict[str, Any]:
        uid = user_id or get_user_id()
        target = (url or "").strip()
        if not target:
            raise ValueError("empty url")

        def _op() -> dict[str, Any]:
            with self._lock:
                sess = self._sessions.get(uid)
            if not sess or not sess.get("page"):
                # Must create on this same worker thread.
                return self._ensure_on_worker(uid, target, headless=False)
            self._navigate_unlocked(sess, target)
            with self._lock:
                return self._public(sess)

        return _WORKER.call(_op, timeout=90.0)

    def screenshot_png(
        self,
        *,
        user_id: Optional[str] = None,
        full_page: bool = False,
    ) -> bytes:
        uid = user_id or get_user_id()

        def _op() -> bytes:
            with self._lock:
                page = self._page_unlocked(uid)
            return page.screenshot(full_page=full_page, type="png")

        return _WORKER.call(_op, timeout=60.0)

    def console_logs(
        self,
        *,
        user_id: Optional[str] = None,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        uid = user_id or get_user_id()
        with self._lock:
            sess = self._sessions.get(uid)
            if not sess:
                return []
            logs = list(sess.get("console") or [])
            return logs[-max(1, min(limit, 200)) :]

    def pick_element(
        self,
        *,
        user_id: Optional[str] = None,
        timeout_ms: int = 60000,
        with_screenshot: bool = True,
    ) -> Optional[DomElementPayload]:
        uid = user_id or get_user_id()
        wait_s = max(5.0, (int(timeout_ms) / 1000.0) + 15.0)

        def _op() -> Optional[DomElementPayload]:
            with self._lock:
                page = self._page_unlocked(uid)
            page.evaluate(_SELECT_BOOTSTRAP)
            raw = page.evaluate(
                "(timeoutMs) => window.__sidekickSelectArm(timeoutMs)",
                int(timeout_ms),
            )
            if not raw or not isinstance(raw, dict):
                return None
            payload = DomElementPayload.model_validate(raw)
            if with_screenshot:
                try:
                    handle = None
                    if payload.xpath:
                        try:
                            handle = page.query_selector(f"xpath={payload.xpath}")
                        except Exception:
                            handle = None
                    if handle is None and payload.css_path:
                        try:
                            handle = page.query_selector(payload.css_path)
                        except Exception:
                            handle = None
                    if handle is not None:
                        png = handle.screenshot(type="png")
                        payload.screenshot_base64 = base64.b64encode(png).decode("ascii")
                except Exception:
                    pass
            if payload.component is None:
                payload.component = DomComponentHint()
            if not isinstance(payload.rect, DomRect):
                payload.rect = DomRect.model_validate(payload.rect or {})
            return payload

        return _WORKER.call(_op, timeout=wait_s)

    def cancel_pick(self, *, user_id: Optional[str] = None) -> None:
        uid = user_id or get_user_id()

        def _op() -> None:
            with self._lock:
                sess = self._sessions.get(uid)
                page = sess.get("page") if sess else None
            if not page:
                return
            try:
                page.evaluate(
                    "() => { if (window.__sidekickSelectCancel) window.__sidekickSelectCancel(); }"
                )
            except Exception:
                pass

        try:
            _WORKER.call(_op, timeout=10.0)
        except Exception:
            pass

    def click_selector(self, selector: str, *, user_id: Optional[str] = None) -> str:
        uid = user_id or get_user_id()
        sel = (selector or "").strip()
        if not sel:
            return "ERROR: empty selector"

        def _op() -> str:
            with self._lock:
                page = self._page_unlocked(uid)
                sess = self._sessions[uid]
            page.click(sel, timeout=15000)
            with self._lock:
                sess["url"] = page.url
            return f"clicked {sel} @ {page.url}"

        return _WORKER.call(_op, timeout=30.0)

    def type_text(
        self,
        selector: str,
        text: str,
        *,
        user_id: Optional[str] = None,
        clear: bool = True,
    ) -> str:
        uid = user_id or get_user_id()
        sel = (selector or "").strip()
        if not sel:
            return "ERROR: empty selector"

        def _op() -> str:
            with self._lock:
                page = self._page_unlocked(uid)
            if clear:
                page.fill(sel, text or "", timeout=15000)
            else:
                page.type(sel, text or "", timeout=15000)
            return f"typed into {sel}"

        return _WORKER.call(_op, timeout=30.0)

    def save_screenshot_to_workspace(
        self,
        workspace: Path,
        *,
        user_id: Optional[str] = None,
        name: str = "",
        full_page: bool = False,
    ) -> Path:
        png = self.screenshot_png(user_id=user_id, full_page=full_page)
        out_dir = Path(workspace) / ".sidekick" / "browser"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = (name or f"shot_{int(time.time())}.png").replace("..", "")
        if not fname.endswith(".png"):
            fname += ".png"
        path = out_dir / fname
        path.write_bytes(png)
        return path

    def _ensure_on_worker(self, uid: str, url: str, *, headless: bool) -> dict[str, Any]:
        """Create session; must already be running on the Playwright worker thread."""
        from playwright.sync_api import sync_playwright

        with self._lock:
            existing = self._sessions.get(uid)
            if existing and existing.get("page"):
                if url:
                    self._navigate_unlocked(existing, url)
                return self._public(existing)

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.on("console", lambda msg: self._on_console(uid, msg))
        go = url or "about:blank"
        if go != "about:blank":
            page.goto(go, wait_until="domcontentloaded", timeout=60000)
        sess = {
            "user_id": uid,
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "url": page.url,
            "started_at": time.time(),
            "console": [],
            "host": HOST_KIND,
        }
        with self._lock:
            self._sessions[uid] = sess
            return self._public(sess)

    def _page_unlocked(self, uid: str) -> Any:
        sess = self._sessions.get(uid)
        if not sess or not sess.get("page"):
            raise RuntimeError(
                "browser session not started — open a URL in the Browser panel first "
                "(or Ctrl+click a link → Open in sandbox)"
            )
        return sess["page"]

    def _navigate_unlocked(self, sess: dict[str, Any], url: str) -> None:
        page = sess["page"]
        # Prefer networkidle-ish readiness for Vite/Vue apps; fall back soft.
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            page.goto(url, wait_until="commit", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        sess["url"] = page.url

    def _public(self, sess: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": HOST_KIND,
            "url": sess.get("url") or "about:blank",
            "started_at": sess.get("started_at"),
            "ready": True,
        }

    def _on_console(self, uid: str, msg: Any) -> None:
        try:
            entry = {
                "type": getattr(msg, "type", "") or "",
                "text": (getattr(msg, "text", "") or "")[:2000],
                "ts": time.time(),
            }
        except Exception:
            return
        with self._lock:
            sess = self._sessions.get(uid)
            if not sess:
                return
            logs = sess.setdefault("console", [])
            logs.append(entry)
            if len(logs) > 300:
                del logs[: len(logs) - 300]


SANDBOX = BrowserSandbox()
