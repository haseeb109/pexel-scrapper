"""Chrome mode, end to end: real Chrome (Playwright) against the mock site, with a
simulated Pexels bot-check on the first visit and the video CDN refusing direct downloads.
Linux needs a display:  xvfb-run python tests/test_chrome.py
"""
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
from mock_server import BASE, PORT, H, FLAGS  # noqa: E402

os.environ.setdefault("CHROME_PATH", shutil.which("google-chrome") or "")
os.environ["PEXELS_CHROME_PROFILE"] = tempfile.mkdtemp()
os.environ["PEXELS_CHECK_WAIT"] = "40"
import script_broll as sb  # noqa: E402

SCRIPT = "The courtroom fell silent as the defendant stood up. Outside, rain hammered against the windows."


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = Path(tempfile.mkdtemp())
    logs = []
    try:
        FLAGS.update(CHALLENGE=True, BLOCK_FILES=True)
        res = sb.run_project(SCRIPT, out, "chrome-test",
                             sb.Settings(delay=0.5, use_browser=True, clips_per_sentence=1),
                             deepseek_key=None, site_base=BASE,
                             log=lambda m: (logs.append(m), print(m)))
        print("result:", res)
        assert FLAGS["challenges_shown"] >= 1, "the check page should have been shown"
        assert any(m.startswith("⚠ Pexels is showing a check") for m in logs), "user told to complete the check"
        assert any(m.startswith("✓ Check passed") for m in logs), "app continued after the check"
        assert res["sentences"] == 2 and res["clips"] == 2 and not res["missing"], res
        assert FLAGS["browser_downloads"] == 2, "blocked direct downloads should go through Chrome"
        clips = sorted(Path(res["folder"], "clips").glob("*.mp4"))
        assert [c.name[:4] for c in clips] == ["001a", "002a"] and all(c.stat().st_size == 20000 for c in clips)
        assert not list(Path(res["folder"], "clips").glob("*.part")), "no leftover partial files"
        # a crashed earlier run leaves Chrome's profile lock behind → app must still start
        prof = Path(os.environ["PEXELS_CHROME_PROFILE"])
        lock = prof / "SingletonLock"
        if lock.exists() or lock.is_symlink():
            lock.unlink()
        os.symlink("some-other-mac-99999", lock)
        from browser_site import BrowserSite
        site = BrowserSite(BASE, delay=0, log=print)
        data = site.search_page("courtroom")
        site.close()
        assert data["videos"], "launch should recover from a stale profile lock"
        print("ALL CHROME TESTS PASSED")
    finally:
        srv.shutdown()
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    main()
