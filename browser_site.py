"""
Pexels through your real Google Chrome.

Pexels' bot-check blocks plain Python requests, so this opens a visible Chrome
window (its own profile, kept between runs), loads each search page there like
a normal visit, and reads the same __NEXT_DATA__ JSON the page ships.

If Pexels shows a check ("Just a moment…" / "Verify you are human"), YOU
complete it in that Chrome window. The app waits, and because the profile is
kept, you usually only need to do it once in a while.

No stealth tricks, no fingerprint spoofing, no automatic CAPTCHA solving.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urlencode

import pexels_scraper as ps

PROFILE_DIR = Path(os.getenv("PEXELS_CHROME_PROFILE", Path.home() / ".pexels_broll_chrome"))
CHECK_WAIT_SECONDS = int(os.getenv("PEXELS_CHECK_WAIT", 300))

READ_JS = """() => {
  const el = document.getElementById('__NEXT_DATA__');
  return el ? el.textContent : null;
}"""
CHECK_JS = """() => {
  const t = (document.title || '').toLowerCase();
  const b = (document.body && document.body.innerText || '').toLowerCase().slice(0, 4000);
  return t.includes('just a moment') || t.includes('attention required') ||
         b.includes('verify you are human') || b.includes('checking your browser') ||
         !!document.querySelector('#challenge-form, #cf-challenge-running, iframe[src*="challenges.cloudflare.com"]');
}"""


class BrowserUnavailable(RuntimeError):
    pass


class BrowserSite:
    """Drop-in replacement for pexels_scraper.PexelsSite (same search_page())."""

    def __init__(self, base: str = ps.SITE, delay: float = 2.0,
                 log: Callable[[str], None] = print, stop_event=None, verbose: bool = True):
        self.base = base.rstrip("/")
        self.delay = delay
        self.log = log if verbose else (lambda m: None)
        self.stop_event = stop_event
        self._pw = self._ctx = self._page = None
        self._last = 0.0

    # ------------------------------------------------------------- lifecycle
    def _launch(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable("Playwright is not installed — run: pip install playwright") from e
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        opts = dict(user_data_dir=str(PROFILE_DIR), headless=False,
                    viewport={"width": 1100, "height": 760}, accept_downloads=True)
        exe = os.getenv("CHROME_PATH")
        attempts = ([dict(executable_path=exe)] if exe else []) + [dict(channel="chrome"), {}]
        last = None
        for extra in attempts:
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(**opts, **extra)
                break
            except Exception as e:  # noqa: BLE001
                last = e
        if self._ctx is None:
            # Neither Google Chrome nor Playwright's Chromium is available → fetch Chromium once
            self.log("Google Chrome not found — downloading a browser for the app (one time, ~150 MB)…")
            r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                self.close()
                raise BrowserUnavailable(f"Could not start Chrome: {last}\n{r.stderr[-400:]}")
            self._ctx = self._pw.chromium.launch_persistent_context(**opts)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.log("Chrome window opened for Pexels (keep it open while the app works).")

    def close(self):
        for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._ctx = self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------------------- pages
    def _wait_gap(self):
        gap = self.delay + random.uniform(0, self.delay * 0.5) - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def _stopped(self) -> bool:
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _read_next_data(self) -> Optional[str]:
        try:
            return self._page.evaluate(READ_JS)
        except Exception:  # noqa: BLE001 — page navigating
            return None

    def _on_check(self) -> bool:
        try:
            return bool(self._page.evaluate(CHECK_JS))
        except Exception:  # noqa: BLE001
            return False

    def search_page(self, keyword: str, page: int = 1, orientation: str = None,
                    extra: Optional[dict] = None) -> dict:
        if self._page is None:
            self._launch()
        params = {}
        if page > 1:
            params["page"] = page
        if orientation:
            params["orientation"] = orientation
        params.update(extra or {})
        url = f"{self.base}/search/videos/{quote(keyword)}/" + (f"?{urlencode(params)}" if params else "")

        for attempt in range(1, 4):
            self._wait_gap()
            try:
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:  # noqa: BLE001
                self.log(f"  page load problem ({e.__class__.__name__}), retry {attempt}/3")
                time.sleep(3 * attempt)
                continue
            if resp is not None and resp.status == 404:
                return {"videos": [], "total_pages": 0, "total_results": 0, "related": []}
            if resp is not None and resp.status == 429:
                self.log("  Pexels says slow down (429) — waiting 60s")
                time.sleep(60)
                continue

            raw = self._read_next_data()
            if raw is None and self._on_check():
                raw = self._wait_for_human()
            if raw is None:
                # page may still be hydrating — give it a few seconds
                for _ in range(10):
                    time.sleep(0.5)
                    raw = self._read_next_data()
                    if raw:
                        break
            if raw:
                return ps.PexelsSite._parse(f'<script id="__NEXT_DATA__">{raw}</script>')
            if self._on_check():
                raise ps.Blocked("Pexels check was not completed in the Chrome window")
        raise RuntimeError(f"Could not read Pexels results for '{keyword}' (page layout may have changed)")

    def _wait_for_human(self) -> Optional[str]:
        try:
            self._page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        self.log("⚠ Pexels is showing a check in the Chrome window — please complete it there. Waiting…")
        t0 = time.time()
        last_note = t0
        while time.time() - t0 < CHECK_WAIT_SECONDS:
            if self._stopped():
                return None
            time.sleep(2)
            raw = self._read_next_data()
            if raw:
                self.log("✓ Check passed — continuing.")
                return raw
            if time.time() - last_note > 30:
                left = int(CHECK_WAIT_SECONDS - (time.time() - t0))
                self.log(f"  still waiting for the Pexels check in Chrome… ({left}s left)")
                last_note = time.time()
        return None

    # ------------------------------------------------------------- downloads
    def download_via_browser(self, url: str, dest: Path, referer: str = "") -> bool:
        """Fallback: let Chrome download the file itself, like pressing Pexels' Download button."""
        if self._page is None:
            return False
        try:
            with self._page.expect_download(timeout=180000) as info:
                try:
                    self._page.goto(url, timeout=60000)
                except Exception:  # noqa: BLE001 — "Download is starting" aborts the navigation
                    pass
            info.value.save_as(str(dest))
            return dest.exists() and dest.stat().st_size > 0
        except Exception as e:  # noqa: BLE001
            self.log(f"  browser download failed: {e.__class__.__name__}")
            return False
