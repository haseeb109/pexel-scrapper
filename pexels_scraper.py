#!/usr/bin/env python3
"""
Pexels video scraper — no API key needed.

Reads the public search pages (https://www.pexels.com/search/videos/<keyword>/),
pulls the JSON the page ships in <script id="__NEXT_DATA__">, filters + ranks
the videos, and downloads the best HD/4K MP4 of each.

    pip install requests
    python pexels_scraper.py "ocean waves" "city night" -n 10 --resolution 1080

See README.md for every option and the full field map.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

SITE = "https://www.pexels.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ORIENTATIONS = ("landscape", "portrait", "square")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def res_label(short_side: int) -> str:
    """Label from the SHORT side, so a 1080x1920 vertical counts as 1080p."""
    for px, lab in ((2160, "4K"), (1440, "1440p"), (1080, "1080p"), (720, "720p"),
                    (540, "540p"), (360, "360p")):
        if short_side >= px:
            return lab
    return f"{short_side}p"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class VideoFile:
    quality: Optional[str]          # 'hd' | 'sd' | 'uhd' | None (null seen on live site)
    file_type: str
    width: int
    height: int
    fps: float
    link: str                       # direct MP4 on videos.pexels.com
    download_link: str = ""         # pexels.com/download/... redirect (fallback)

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    @property
    def usable(self) -> bool:
        q = (self.quality or "").lower()
        return q != "hls" and "mpegurl" not in self.file_type.lower() \
            and bool(self.width and self.height and (self.link or self.download_link))


@dataclass
class Video:
    id: int
    keyword: str
    title: str
    description: str
    pexels_url: str
    width: int
    height: int
    aspect_ratio: float
    orientation: str
    duration: int
    fps: float
    max_resolution: str
    resolutions: list[str]
    thumbnail: str
    preview_url: str
    author_name: str
    author_username: str
    author_url: str
    tags: list[str]
    created_at: str
    files: list[VideoFile] = field(repr=False)
    score: float = 0.0
    chosen_file: Optional[VideoFile] = None
    local_path: str = ""
    status: str = "pending"

    @classmethod
    def from_site(cls, item: dict, keyword: str) -> "Video":
        """Map one entry of pageProps.initialData.data[] (captured from pexels.com)."""
        a = item.get("attributes") or item
        vid = a.get("video") or {}
        files = [VideoFile(
            quality=f.get("quality"), file_type=f.get("file_type") or "",
            width=int(f.get("width") or 0), height=int(f.get("height") or 0),
            fps=float(f.get("fps") or 0), link=f.get("link") or "",
            download_link=f.get("download_link") or "",
        ) for f in vid.get("video_files") or []]
        good = [f for f in files if f.usable]
        vid_id = int(a.get("id") or item.get("id"))
        w, h = int(a.get("width") or 0), int(a.get("height") or 0)
        ar = float(a.get("aspect_ratio") or (w / h if h else 0))
        orient = "square" if 0.95 <= ar <= 1.05 else ("landscape" if ar > 1 else "portrait")
        slug = (a.get("slug") or "").strip("/")
        if slug and not slug.endswith(str(vid_id)):
            slug = f"{slug}-{vid_id}"
        url = f"{SITE}/video/{slug or vid_id}/"
        title = (a.get("title") or "").strip() or \
            re.sub(r"-?\d+$", "", slug).replace("-", " ").strip().capitalize() or f"Pexels video {vid_id}"
        u = a.get("user") or {}
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
        uname = u.get("username") or u.get("slug") or ""
        tags = [t.get("name", "") if isinstance(t, dict) else str(t) for t in a.get("tags") or []]
        shorts = sorted({f.short_side for f in good}, reverse=True)
        return cls(
            id=vid_id, keyword=keyword, title=title, description=(a.get("description") or "").strip(),
            pexels_url=url, width=w, height=h, aspect_ratio=round(ar, 4), orientation=orient,
            duration=int(a.get("duration") or 0),
            fps=float(a.get("fps") or max((f.fps for f in good), default=0)),
            max_resolution=res_label(shorts[0]) if shorts else "n/a",
            resolutions=[res_label(s) for s in shorts],
            thumbnail=(vid.get("thumbnail") or {}).get("large", "") if isinstance(vid.get("thumbnail"), dict)
            else (vid.get("thumbnail") or ""),
            preview_url=vid.get("preview_src") or "",
            author_name=name or uname, author_username=uname,
            author_url=f"{SITE}/@{uname}/" if uname else "",
            tags=[t for t in tags if t], created_at=a.get("created_at") or "", files=files,
        )

    def row(self) -> dict:
        d = asdict(self)
        d.pop("files")
        cf = self.chosen_file
        d["chosen_file"] = f"{cf.width}x{cf.height}@{cf.fps:g}fps" if cf else ""
        d["download_url"] = (cf.link or cf.download_link) if cf else ""
        d["resolutions"] = "|".join(self.resolutions)
        d["tags"] = "|".join(self.tags)
        return d


# --------------------------------------------------------------------------- #
# Site client
# --------------------------------------------------------------------------- #
class Blocked(RuntimeError):
    pass


class PexelsSite:
    def __init__(self, base: str = SITE, delay: float = 2.0, max_retries: int = 4, verbose: bool = True):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay, self.max_retries, self.verbose = delay, max_retries, verbose
        self._last = 0.0

    def _log(self, m):
        if self.verbose:
            print(m, file=sys.stderr)

    def _polite_wait(self):
        gap = self.delay + random.uniform(0, self.delay * 0.5) - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def search_page(self, keyword: str, page: int = 1, orientation: str = None,
                    extra: Optional[dict] = None) -> dict:
        url = f"{self.base}/search/videos/{quote(keyword)}/"
        params = {}
        if page > 1:      params["page"] = page
        if orientation:   params["orientation"] = orientation
        params.update(extra or {})
        for attempt in range(1, self.max_retries + 1):
            self._polite_wait()
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                self._log(f"  network error: {e} (retry {attempt})")
                time.sleep(3 * attempt)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After") or 0) or 30 * attempt
                self._log(f"  429 too many requests — waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(3 * attempt)
                continue
            body = r.text
            if r.status_code in (403, 503) or "Just a moment" in body[:3000] or "cf-chl" in body[:5000]:
                raise Blocked(f"Pexels returned a bot-check page (HTTP {r.status_code}) for {r.url}")
            if r.status_code == 404:
                return {"videos": [], "total_pages": 0, "total_results": 0}
            r.raise_for_status()
            return self._parse(body)
        raise RuntimeError(f"Giving up on {url} page {page}")

    @staticmethod
    def _parse(html: str) -> dict:
        m = NEXT_DATA_RE.search(html)
        if not m:
            raise RuntimeError("Page layout changed: __NEXT_DATA__ not found")
        pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        init = pp.get("initialData") or {}
        pag = init.get("pagination") or {}
        meta = init.get("meta") or {}
        return {
            "videos": [x for x in init.get("data") or [] if (x.get("type") in (None, "video"))],
            "total_pages": pag.get("total_pages") or 1,
            "total_results": pag.get("total_results") or meta.get("total_results") or 0,
            "related": [r.get("name", r) if isinstance(r, dict) else r for r in meta.get("related_searches") or []],
        }


# --------------------------------------------------------------------------- #
# Filtering, file choice, ranking
# --------------------------------------------------------------------------- #
@dataclass
class Criteria:
    min_duration: int = 0
    max_duration: int = 0
    min_width: int = 0
    min_height: int = 0
    min_fps: float = 0
    max_fps: float = 0
    aspect: Optional[float] = None
    aspect_tol: float = 0.03
    resolution: int = 1080
    strict_resolution: bool = False
    target_duration: int = 0
    require_tags: list[str] = field(default_factory=list)
    exclude_words: list[str] = field(default_factory=list)
    exclude_authors: list[str] = field(default_factory=list)


def choose_file(v: Video, c: Criteria) -> Optional[VideoFile]:
    """Exact target res > smallest above target > (unless strict) best below. Higher fps breaks ties."""
    good = [f for f in v.files if f.usable]
    if not good:
        return None
    exact = [f for f in good if f.short_side == c.resolution]
    if exact:
        return max(exact, key=lambda f: f.fps)
    above = [f for f in good if f.short_side > c.resolution]
    if above:
        lo = min(f.short_side for f in above)
        return max((f for f in above if f.short_side == lo), key=lambda f: f.fps)
    if c.strict_resolution:
        return None
    hi = max(f.short_side for f in good)
    return max((f for f in good if f.short_side == hi), key=lambda f: f.fps)


def passes(v: Video, c: Criteria) -> tuple[bool, str]:
    if c.min_duration and v.duration < c.min_duration:   return False, "too short"
    if c.max_duration and v.duration > c.max_duration:   return False, "too long"
    if c.min_width and v.width < c.min_width:             return False, "width"
    if c.min_height and v.height < c.min_height:          return False, "height"
    if c.min_fps and v.fps < c.min_fps:                   return False, "fps low"
    if c.max_fps and v.fps > c.max_fps:                   return False, "fps high"
    if c.aspect and abs(v.aspect_ratio - c.aspect) > c.aspect_tol:
        return False, "aspect"
    hay = " ".join([v.title, v.description, *v.tags]).lower()
    if c.require_tags and not all(t.lower() in hay for t in c.require_tags):
        return False, "missing word"
    if any(w.lower() in hay for w in c.exclude_words):    return False, "excluded word"
    if {v.author_name.lower(), v.author_username.lower()} & {x.lower() for x in c.exclude_authors}:
        return False, "excluded author"
    v.chosen_file = choose_file(v, c)
    if v.chosen_file is None:
        return False, "no file at target res"
    return True, ""


def score(v: Video, c: Criteria) -> float:
    """0–100: keyword match 40, top resolution 30, duration fit 20, fps 10."""
    kw = [t for t in re.split(r"\W+", v.keyword.lower()) if t]
    hay = " ".join([v.title, v.description, *v.tags]).lower()
    rel = sum(t in hay for t in kw) / len(kw) if kw else 0
    top = max((f.short_side for f in v.files if f.usable), default=0)
    if c.target_duration:
        dur = max(0.0, 1 - abs(v.duration - c.target_duration) / max(c.target_duration, 1))
    else:
        dur = 1.0 if 8 <= v.duration <= 60 else 0.5
    return round(rel * 40 + min(top / 2160, 1) * 30 + dur * 20 + min(v.fps / 60, 1) * 10, 2)


def collect(site: PexelsSite, keyword: str, want: int, c: Criteria, orientation=None,
            max_pages: int = 8, seen: Optional[set] = None, extra=None, verbose=True) -> list[Video]:
    """~24 results per page; the site can't filter duration/fps/aspect, so over-fetch and filter here."""
    seen = seen if seen is not None else set()
    kept, rejected, prev_ids = [], {}, None
    for page in range(1, max_pages + 1):
        data = site.search_page(keyword, page, orientation, extra)
        items = data["videos"]
        if page == 1 and verbose:
            rel = f" | related: {', '.join(map(str, data['related'][:6]))}" if data["related"] else ""
            print(f"[{keyword}] {data['total_results']} results on Pexels{rel}", file=sys.stderr)
        ids = [str(x.get("id")) for x in items]
        if not items or ids == prev_ids:          # end of results / page param ignored
            break
        prev_ids = ids
        for it in items:
            try:
                v = Video.from_site(it, keyword)
            except Exception:  # noqa: BLE001 — skip malformed entries
                rejected["unparseable"] = rejected.get("unparseable", 0) + 1
                continue
            if v.id in seen:
                rejected["duplicate"] = rejected.get("duplicate", 0) + 1
                continue
            ok, why = passes(v, c)
            if not ok:
                rejected[why] = rejected.get(why, 0) + 1
                continue
            v.score = score(v, c)
            kept.append(v)
            seen.add(v.id)
        if len(kept) >= want * 2 or page >= data["total_pages"]:
            break
    kept.sort(key=lambda v: v.score, reverse=True)
    if verbose:
        rej = ", ".join(f"{k}: {n}" for k, n in sorted(rejected.items(), key=lambda x: -x[1]))
        print(f"[{keyword}] kept {len(kept)} → using top {min(want, len(kept))}"
              + (f"  (rejected — {rej})" if rej else ""), file=sys.stderr)
    return kept[:want]


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def safe_name(s: str, n: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:n].strip("-") or "video"


_lock = threading.Lock()


def download(v: Video, out_dir: Path, session: requests.Session, retries: int = 4) -> Video:
    f = v.chosen_file
    folder = out_dir / safe_name(v.keyword)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{v.id}_{safe_name(v.title, 50)}_{f.width}x{f.height}.mp4"
    dest, part = folder / name, folder / (name + ".part")
    v.local_path = str(dest)
    if dest.exists() and dest.stat().st_size > 0:
        v.status = "exists"
        return v
    urls = [u for u in (f.link, f.download_link) if u]
    for attempt in range(1, retries + 1):
        url = urls[min(attempt - 1, len(urls) - 1)] if attempt > 2 else urls[0]
        try:
            have = part.stat().st_size if part.exists() else 0
            hdr = {"Referer": v.pexels_url, **({"Range": f"bytes={have}-"} if have else {})}
            with session.get(url, headers=hdr, stream=True, timeout=60, allow_redirects=True) as r:
                if r.status_code == 416:
                    part.rename(dest); v.status = "downloaded"; return v
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if "text/html" in ctype:
                    raise IOError("got an HTML page instead of video")
                mode = "ab" if (have and r.status_code == 206) else "wb"
                total = int(r.headers.get("Content-Length") or 0) + (have if mode == "ab" else 0)
                with open(part, mode) as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
            got = part.stat().st_size
            if total and got < total:
                raise IOError(f"incomplete {got}/{total} bytes")
            part.rename(dest)
            v.status = "downloaded"
            with _lock:
                print(f"  ✓ {dest.relative_to(out_dir)}  ({got / 1e6:.1f} MB)", file=sys.stderr)
            return v
        except Exception as e:  # noqa: BLE001
            with _lock:
                print(f"  ! {v.id} attempt {attempt}/{retries}: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    v.status = "failed"
    return v


# --------------------------------------------------------------------------- #
# Outputs / resume
# --------------------------------------------------------------------------- #
def load_done(out_dir: Path) -> set[int]:
    p, done = out_dir / "manifest.jsonl", set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("status") in ("downloaded", "exists") and Path(r.get("local_path", "")).exists():
                    done.add(int(r["id"]))
            except Exception:  # noqa: BLE001
                pass
    return done


COLS = ["id", "keyword", "score", "title", "status", "local_path", "chosen_file", "max_resolution",
        "resolutions", "width", "height", "aspect_ratio", "orientation", "duration", "fps",
        "author_name", "author_url", "pexels_url", "thumbnail", "preview_url", "tags",
        "description", "created_at", "download_url"]


def write_outputs(videos: list[Video], out_dir: Path):
    rows = [v.row() for v in videos]
    with open(out_dir / "manifest.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    p = out_dir / "results.csv"
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        if new: w.writeheader()
        w.writerows(rows)
    with open(out_dir / "CREDITS.txt", "a", encoding="utf-8") as fh:
        for v in videos:
            if v.status in ("downloaded", "exists"):
                fh.write(f"{v.title} — video by {v.author_name} on Pexels: {v.pexels_url}\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_aspect(s):
    if not s: return None
    if ":" in s:
        a, b = s.split(":"); return float(a) / float(b)
    return float(s)


def build_parser():
    p = argparse.ArgumentParser(description="Scrape, rank and download Pexels videos by keyword (no API key).",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("keywords", nargs="*")
    p.add_argument("-k", "--keywords-file", help="one keyword per line")
    p.add_argument("-n", "--per-keyword", type=int, default=5)
    p.add_argument("-o", "--out", default="pexels_downloads")
    p.add_argument("--site", default=SITE, help=argparse.SUPPRESS)  # testing

    g = p.add_argument_group("search")
    g.add_argument("--orientation", choices=ORIENTATIONS, help="sent to Pexels in the URL")
    g.add_argument("--site-param", nargs="+", default=[], metavar="KEY=VALUE",
                   help="extra URL params passed to the search page as-is (experimental)")
    g.add_argument("--max-pages", type=int, default=8, help="search pages per keyword (~24 videos each)")
    g.add_argument("--delay", type=float, default=2.0, help="seconds between page requests (+ random jitter)")
    g.add_argument("--browser", action="store_true",
                   help="load Pexels through a visible Chrome window (use when Pexels blocks plain requests)")

    g = p.add_argument_group("filters (applied to results)")
    g.add_argument("--min-duration", type=int, default=0)
    g.add_argument("--max-duration", type=int, default=0)
    g.add_argument("--min-width", type=int, default=0)
    g.add_argument("--min-height", type=int, default=0)
    g.add_argument("--min-fps", type=float, default=0)
    g.add_argument("--max-fps", type=float, default=0)
    g.add_argument("--aspect", help="16:9, 9:16, 1:1, 2.39 …")
    g.add_argument("--aspect-tol", type=float, default=0.03)
    g.add_argument("--require", nargs="+", default=[], metavar="WORD")
    g.add_argument("--exclude", nargs="+", default=[], metavar="WORD")
    g.add_argument("--exclude-author", nargs="+", default=[], metavar="NAME")

    g = p.add_argument_group("quality & ranking")
    g.add_argument("--resolution", type=int, default=1080, choices=(720, 1080, 1440, 2160))
    g.add_argument("--strict-resolution", action="store_true")
    g.add_argument("--target-duration", type=int, default=0)

    g = p.add_argument_group("run")
    g.add_argument("--dry-run", action="store_true", help="scrape + rank + CSV, no downloads")
    g.add_argument("--workers", type=int, default=3)
    g.add_argument("--quiet", action="store_true")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    kws = list(a.keywords)
    if a.keywords_file:
        kws += [l.strip() for l in Path(a.keywords_file).read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")]
    if not kws:
        sys.exit("Give at least one keyword (or -k keywords.txt).")
    extra = dict(kv.split("=", 1) for kv in a.site_param)
    c = Criteria(a.min_duration, a.max_duration, a.min_width, a.min_height, a.min_fps, a.max_fps,
                 parse_aspect(a.aspect), a.aspect_tol, a.resolution, a.strict_resolution,
                 a.target_duration, a.require, a.exclude, a.exclude_author)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.browser:
        from browser_site import BrowserSite
        site = BrowserSite(a.site, delay=a.delay, verbose=not a.quiet)
    else:
        site = PexelsSite(a.site, delay=a.delay, verbose=not a.quiet)
    seen = load_done(out)
    if seen and not a.quiet:
        print(f"Resuming: {len(seen)} already downloaded in {out}/ — skipping those", file=sys.stderr)

    picked: list[Video] = []
    for kw in kws:
        try:
            picked += collect(site, kw, a.per_keyword, c, a.orientation, a.max_pages, seen, extra, not a.quiet)
        except Blocked as e:
            print(f"\n✗ {e}\n  Pexels is blocking automated requests from this connection right now. "
                  "Wait a while, raise --delay, or try another network.", file=sys.stderr)
            break

    if hasattr(site, "close") and a.dry_run:
        site.close()
    if picked and not a.dry_run:
        dl = requests.Session(); dl.headers.update(HEADERS)
        print(f"Downloading {len(picked)} videos at ~{res_label(a.resolution)} …", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            for _ in as_completed([ex.submit(download, v, out, dl) for v in picked]):
                pass
    elif a.dry_run:
        for v in picked: v.status = "dry-run"

    if a.browser and not a.dry_run:
        for v in picked:
            if v.status == "failed":
                dest = Path(v.local_path)
                cf = v.chosen_file
                if site.download_via_browser(cf.download_link or cf.link, dest, referer=v.pexels_url):
                    v.status = "downloaded"
        site.close()
    write_outputs(picked, out)
    ok = sum(v.status in ("downloaded", "exists") for v in picked)
    bad = sum(v.status == "failed" for v in picked)
    print(f"\nDone. {len(picked)} selected, {ok} on disk, {bad} failed → {out}/results.csv", file=sys.stderr)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
