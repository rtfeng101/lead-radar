"""One shared, visible Chrome window for talking to gaf.com.

Why a real browser: gaf.com sits behind Akamai bot protection. Plain HTTP clients (curl / httpx) and headless
browsers (including headless Chrome) get `403 Access Denied`; a normal headed Chrome gets 200. We drive the
user's installed Chrome (`channel="chrome"`, nothing to download) and do NOT spoof fingerprints or rotate
proxies: if GAF changes its protection, the answer is a data agreement with GAF, not an arms race.

Consequence for production: this needs a display (a desktop, or Xvfb on a server) and must stay slow and polite.
It is confined to `profile` jobs so it can run as a small, separate worker pool (`--kinds profile`).
"""

import atexit
import logging
import threading
import time

from app.config import settings

log = logging.getLogger(__name__)


class GafBlocked(RuntimeError):
    """Akamai refused the request (403 / 'Access Denied'). Transient from the queue's point of view."""


class GafBrowser:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None
        self._last_nav = 0.0
        self._lock = threading.Lock()

    def _ensure(self):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright  # imported lazily: only live-GAF runs need it

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(channel=settings.gaf_browser_channel, headless=False)
        except Exception as e:  # noqa: BLE001
            self._pw.stop()
            self._pw = None
            raise RuntimeError(
                f"could not launch Chrome (channel={settings.gaf_browser_channel!r}); is Google Chrome installed?"
            ) from e
        ctx = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self._page = ctx.new_page()
        atexit.register(self.close)
        return self._page

    def close(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001 - shutting down; nothing useful to do
                pass
        self._page = self._browser = self._pw = None

    def _throttle(self) -> None:
        wait = settings.gaf_page_delay_s - (time.monotonic() - self._last_nav)
        if wait > 0:
            time.sleep(wait)
        self._last_nav = time.monotonic()

    def fetch_html(self, url: str) -> str:
        """Load a gaf.com page and return its rendered HTML."""
        with self._lock:
            page = self._ensure()
            self._throttle()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            status = resp.status if resp else 0
            html = page.content()
            if status == 403 or "<TITLE>Access Denied</TITLE>" in html[:400]:
                raise GafBlocked(f"gaf.com refused {url} (HTTP {status})")
            if status >= 400:
                raise RuntimeError(f"gaf.com returned HTTP {status} for {url}")
            return html

    def capture_coveo_token(self) -> str:
        """Load the contractor directory and return the search token its own JavaScript uses.

        The token is the public key GAF ships to every visitor's browser (it is visible in any DevTools network
        tab); it is fetched at run time and never committed."""
        with self._lock:
            page = self._ensure()
            self._throttle()
            found: list[str] = []

            def on_request(req) -> None:
                auth = req.headers.get("authorization", "")
                if "coveo.com/rest/search" in req.url and auth.startswith("Bearer "):
                    found.append(auth.removeprefix("Bearer "))

            page.on("request", on_request)
            try:
                resp = page.goto(
                    "https://www.gaf.com/en-us/roofing-contractors/residential", wait_until="networkidle", timeout=60_000
                )
            finally:
                page.remove_listener("request", on_request)
            if resp is not None and resp.status == 403:
                raise GafBlocked("gaf.com refused the contractor directory page")
            if not found:
                raise RuntimeError("did not see the directory search request; has the GAF page changed?")
            return found[0]


_browser: GafBrowser | None = None


def get_browser() -> GafBrowser:
    global _browser
    if _browser is None:
        _browser = GafBrowser()
    return _browser
