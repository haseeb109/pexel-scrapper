#!/usr/bin/env python3
"""
Script → B-roll: give it a video script, it finds and downloads Pexels footage
for every sentence, in timeline order.

    python script_broll.py my_script.txt --name episode-12
    python app.py            # same thing with a browser UI + "Download folder" button

DeepSeek (optional, recommended): set DEEPSEEK_API_KEY and each sentence gets
3 visual search queries written by the model. Without it, a simple keyword
extractor is used.
"""
from __future__ import annotations

import argparse
import copy
import functools
import csv
import html
import json
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

import pexels_scraper as ps

DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com") + "/chat/completions"
DEEPSEEK_MODELS = [os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), "deepseek-chat"]
WORDS_PER_SEC = 2.5
BACKOFF = float(os.getenv("DEEPSEEK_BACKOFF", 5))          # seconds; tests set 0          # ~150 wpm narration


# --------------------------------------------------------------------------- #
# 1. Split script into sentences
# --------------------------------------------------------------------------- #
ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|Mt|Lt|Sgt|Capt|Col|Gen|Gov|Sen|Rep|Rev|Hon|"
                    r"vs|etc|approx|No|Vol|Inc|Ltd|Co|Corp|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|"
                    r"Nov|Dec)\.(?=\s)", re.I)
INITIALS = re.compile(r"\b((?:[A-Z]\.){2,})(?=\s+[a-z])")      # U.S. court, D.C. police


def split_script(text: str, min_words: int = 4) -> list[str]:
    text = ABBREV.sub(lambda m: m.group(1) + "\u2024", text)       # protect "Mr." etc. from splitting
    text = INITIALS.sub(lambda m: m.group(1).replace(".", "\u2024"), text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*(music|sfx|pause|intro|outro)[^)]*\)", " ", text, flags=re.I)  # drop [cues]
    parts = []
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        parts += [s.strip() for s in re.split(r"(?<=[.!?۔؟])\s+", para) if s.strip()]
    merged: list[str] = []
    for s in parts:                                   # glue tiny fragments onto the previous line
        if merged and len(s.split()) < min_words:
            merged[-1] += " " + s
        else:
            merged.append(s)
    if len(merged) > 1 and len(merged[0].split()) < min_words:
        merged[1] = merged[0] + " " + merged[1]; merged.pop(0)
    return [m.replace("\u2024", ".") for m in merged]


# --------------------------------------------------------------------------- #
# 2. Turn each sentence into visual search queries
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You are a stock-footage researcher for faceless YouTube videos.
For EACH numbered narration sentence, write 3 Pexels video search queries that would find
b-roll a viewer would accept as illustrating that sentence.

Rules:
- English, 1-4 words each, concrete and filmable (objects, places, actions, moods).
- Most specific first, broadest last (the last one must almost surely exist on a stock site).
- Never use names of real people, brands, TV shows or exact places that stock sites won't have;
  translate them into generic visuals (e.g. "Judge Judy" -> "female judge courtroom").
- Stay consistent with the overall story/setting given in CONTEXT.
- Also give "avoid": 0-3 words for footage that would clash (e.g. "cartoon", "wedding").

Return JSON only: {"items":[{"i":1,"queries":["...","...","..."],"avoid":["..."]}, ...]}"""


class DeepSeekError(RuntimeError):
    pass


class DeepSeekFatal(DeepSeekError):
    """Key rejected / no balance — no point trying more batches."""


def deepseek_queries(sentences: list[str], api_key: str, context: str = "",
                     batch: int = 25, log: Callable[[str], None] = print) -> list[dict]:
    """Ask DeepSeek for visual queries, 25 sentences per call.
    A failed batch falls back to keyword matching for just that batch; a rejected key or
    empty balance on the first batch raises DeepSeekFatal so the caller can fall back for all."""
    out: list[dict] = []
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    state = {"models": list(dict.fromkeys(DEEPSEEK_MODELS)), "thinking": True}
    fatal = ""
    for start in range(0, len(sentences), batch):
        chunk = sentences[start:start + batch]
        data, last_err = None, ""
        if not fatal:
            numbered = "\n".join(f"{start + k + 1}. {t}" for k, t in enumerate(chunk))
            user = f"CONTEXT (start of script):\n{context[:1500]}\n\nSENTENCES:\n{numbered}"
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
            try:
                data, last_err = _deepseek_call(s, msgs, state, log)
            except DeepSeekFatal as e:
                if not out:
                    raise
                fatal = str(e)
                log(f"  ⚠ {e} — remaining sentences use keyword matching")
            except DeepSeekError as e:
                last_err = str(e)
        if data is None:
            if not fatal:
                log(f"  ⚠ DeepSeek failed for sentences {start + 1}-{start + len(chunk)} ({last_err}) "
                    "— using keyword matching for these")
            out += [{"queries": keyword_queries(t, context), "avoid": [], "source": "keywords"} for t in chunk]
            continue
        by_i = {}
        for it in data.get("items", []) if isinstance(data, dict) else []:
            try:
                by_i[int(it.get("i", 0))] = it
            except (TypeError, ValueError, AttributeError):
                pass
        for k, sent in enumerate(chunk):
            it = by_i.get(start + k + 1) or {}
            qs = [q.strip() for q in it.get("queries", []) if isinstance(q, str) and q.strip()][:4]
            out.append({"queries": qs or keyword_queries(sent, context),
                        "avoid": [a for a in it.get("avoid", []) if isinstance(a, str)][:3],
                        "source": "deepseek" if qs else "keywords"})
        log(f"  DeepSeek: queries ready for {min(start + batch, len(sentences))}/{len(sentences)} sentences")
    return out


def _deepseek_call(s: requests.Session, msgs: list, state: dict, log) -> tuple:
    """One batch with retries. Returns (parsed_json | None, last_error)."""
    last_err = ""
    for attempt in range(5):
        body = {"model": state["models"][0], "messages": msgs, "temperature": 0.4, "max_tokens": 4000,
                "response_format": {"type": "json_object"}}
        if state["thinking"]:
            body["thinking"] = {"type": "disabled"}
        try:
            r = s.post(DEEPSEEK_URL, json=body, timeout=120)
        except requests.RequestException as e:
            last_err = str(e); time.sleep(BACKOFF * 0.4 * (attempt + 1)); continue
        if r.status_code == 401:
            raise DeepSeekFatal("DeepSeek rejected the API key (401)")
        if r.status_code == 402:
            raise DeepSeekFatal("DeepSeek account has no balance (402)")
        if r.status_code in (400, 404, 422):
            txt = r.text.lower()
            if "thinking" in txt and state["thinking"]:
                state["thinking"] = False; continue                 # API without that parameter
            if "model" in txt and len(state["models"]) > 1:
                log(f"  model {state['models'][0]} not available, using {state['models'][1]}")
                state["models"].pop(0); continue
        if r.status_code in (429, 500, 502, 503, 504):
            last_err = f"HTTP {r.status_code}"; time.sleep(BACKOFF * (attempt + 1)); continue
        if not r.ok:
            raise DeepSeekError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            return json.loads(r.json()["choices"][0]["message"]["content"]), ""
        except Exception as e:  # noqa: BLE001 — empty/garbled JSON → retry
            last_err = f"bad JSON: {e}"
    return None, last_err


STOP = set("""a an the and or but if then so of to in on at by for with from into onto over under
about after before during while as is are was were be been being am do does did done have has had
i you he she it we they me him her them my your his its our their this that these those there here
what which who whom whose when where why how not no yes very just also too more most much many
some any all each every can could would should will shall may might must said says say tell told
than only even still back out up down off again once one two three first last next new old
really thing things something nothing everything someone anyone everyone because though although
however then now today later already ever never always like get got go going went come came make
made know knew think thought see saw look looked want wanted take took give gave let lets yeah
okay well right left way time day days year years people person man woman men women""".split())


@functools.lru_cache(maxsize=8)
def _topic_word(context: str) -> str:
    """Most frequent content word of the whole script — a safe broad fallback query."""
    words = [w.lower() for w in re.findall(r"[A-Za-z]{5,}", context) if w.lower() not in STOP]
    return max(set(words), key=words.count) if words else ""


def keyword_queries(sentence: str, context: str = "") -> list[str]:
    """Offline fallback: pick the most 'visual' content words."""
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]+", sentence)]
    content = [w.strip("'") for w in words
               if w not in STOP and len(w) > 3 and not w.endswith("ly")]
    seen, uniq = set(), []
    for w in content:
        if w not in seen:
            seen.add(w); uniq.append(w)
    # nouns make better stock searches than verbs: push -ed / -ing words back
    uniq.sort(key=lambda w: (w.endswith(("ed", "ing")), -len(w)))
    top = uniq[:3]
    qs = []
    if len(top) >= 2: qs.append(f"{top[0]} {top[1]}")
    qs += top[:2]
    common = _topic_word(context) if context else ""
    if common:
        qs.append(common)
    return list(dict.fromkeys(qs))[:4] or ["cinematic b-roll"]


# --------------------------------------------------------------------------- #
# 3. Search + pick clips
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    orientation: Optional[str] = "landscape"
    aspect: Optional[float] = 16 / 9
    resolution: int = 1080
    clips_per_sentence: int = 1
    pages_per_query: int = 1
    delay: float = 2.0
    min_clip_seconds: int = 4
    workers: int = 3
    exclude: list[str] = field(default_factory=list)
    use_browser: bool = True          # load Pexels through real Chrome (needed when Pexels blocks Python)


@dataclass
class SentenceResult:
    index: int
    text: str
    seconds: float
    queries: list[str]
    avoid: list[str]
    query_used: str = ""
    clips: list[ps.Video] = field(default_factory=list)
    status: str = "pending"


class FootageFinder:
    def __init__(self, site: ps.PexelsSite, settings: Settings, log: Callable[[str], None] = print):
        self.site, self.st, self.log = site, settings, log
        self.cache: dict[str, list[ps.Video]] = {}
        self.used: set[int] = set()

    def _candidates(self, query: str) -> list[ps.Video]:
        if query not in self.cache:
            vids = []
            for page in range(1, self.st.pages_per_query + 1):
                data = self.site.search_page(query, page, self.st.orientation)
                for it in data["videos"]:
                    try:
                        vids.append(ps.Video.from_site(it, query))
                    except Exception:  # noqa: BLE001
                        pass
                if page >= data["total_pages"]:
                    break
            self.cache[query] = vids
        return self.cache[query]

    def pick(self, sr: SentenceResult) -> SentenceResult:
        need = max(self.st.min_clip_seconds, math.ceil(sr.seconds / self.st.clips_per_sentence))
        # pass 1: clip long enough to cover the line; pass 2: any length
        for min_dur in (need, 0):
            c = ps.Criteria(min_duration=min_dur, aspect=self.st.aspect, aspect_tol=0.15,
                            resolution=self.st.resolution, target_duration=max(need, 8),
                            exclude_words=self.st.exclude + sr.avoid)
            for q in sr.queries:
                pool = []
                for v in self._candidates(q):
                    if v.id in self.used:
                        continue
                    v2 = copy.copy(v); v2.keyword = q
                    ok, _ = ps.passes(v2, c)
                    if ok:
                        v2.score = ps.score(v2, c)
                        pool.append(v2)
                pool.sort(key=lambda v: v.score, reverse=True)
                for v in pool[: self.st.clips_per_sentence - len(sr.clips)]:
                    sr.clips.append(v); self.used.add(v.id)
                if len(sr.clips) >= self.st.clips_per_sentence:
                    sr.query_used = q
                    sr.status = "found"
                    return sr
                if sr.clips and not sr.query_used:
                    sr.query_used = q
        sr.status = "found" if sr.clips else "no match"
        return sr


# --------------------------------------------------------------------------- #
# 4. Run a whole project
# --------------------------------------------------------------------------- #
def slug(s: str, n: int = 40) -> str:
    return ps.safe_name(s, n)


class Cancelled(RuntimeError):
    pass


def test_deepseek_key(api_key: str) -> tuple[bool, str]:
    """Tiny request to check a key works. Returns (ok, message)."""
    if not api_key.strip():
        return False, "No key entered"
    body = {"model": DEEPSEEK_MODELS[0], "max_tokens": 5, "temperature": 0,
            "messages": [{"role": "user", "content": "Reply with OK"}],
            "thinking": {"type": "disabled"}}
    hdr = {"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"}
    try:
        for _ in range(3):
            r = requests.post(DEEPSEEK_URL, json=body, headers=hdr, timeout=30)
            if r.status_code == 400 and "thinking" in r.text.lower() and "thinking" in body:
                body.pop("thinking"); continue
            if r.status_code == 400 and "model" in r.text.lower() and body["model"] != "deepseek-chat":
                body["model"] = "deepseek-chat"; continue
            break
    except requests.RequestException as e:
        return False, f"Could not reach DeepSeek: {e}"
    if r.ok:
        return True, f"Key works ✓ (model: {body['model']})"
    return False, {401: "Key rejected (401) — check you copied the whole key",
                   402: "Key is valid but the account has no balance (402) — top up on platform.deepseek.com",
                   429: "Rate limited (429) — try again in a minute"}.get(r.status_code, f"DeepSeek error {r.status_code}: {r.text[:150]}")


def run_project(script: str, out_root: Path, name: str, settings: Settings,
                deepseek_key: Optional[str] = None, site_base: str = ps.SITE,
                log: Callable[[str], None] = print,
                on_update: Optional[Callable[[list[SentenceResult]], None]] = None,
                stop_event=None) -> dict:
    def check_stop():
        if stop_event is not None and stop_event.is_set():
            raise Cancelled("Stopped by user")

    proj = out_root / slug(name, 50)
    clips_dir = proj / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (proj / "script.txt").write_text(script, encoding="utf-8")

    sentences = split_script(script)
    if not sentences:
        raise ValueError("Script is empty")
    log(f"Script split into {len(sentences)} sentences")

    if deepseek_key:
        log("Asking DeepSeek for visual search queries …")
        try:
            plans = deepseek_queries(sentences, deepseek_key, context=script, log=log)
        except DeepSeekError as e:
            log(f"  ⚠ {e} — falling back to keyword extraction")
            plans = [{"queries": keyword_queries(s, script), "avoid": [], "source": "keywords"} for s in sentences]
    else:
        log("No DeepSeek key — using built-in keyword extraction")
        plans = [{"queries": keyword_queries(s, script), "avoid": [], "source": "keywords"} for s in sentences]

    results = [SentenceResult(i + 1, s, round(len(s.split()) / WORDS_PER_SEC, 1), p["queries"], p["avoid"])
               for i, (s, p) in enumerate(zip(sentences, plans))]
    if on_update: on_update(results)

    if settings.use_browser:
        from browser_site import BrowserSite
        site = BrowserSite(site_base, delay=settings.delay, log=log, stop_event=stop_event)
    else:
        site = ps.PexelsSite(site_base, delay=settings.delay, verbose=False)
    try:
        return _search_and_download(results, site, settings, proj, clips_dir, log, on_update,
                                    stop_event, check_stop)
    finally:
        if hasattr(site, "close"):
            site.close()


def _search_and_download(results, site, settings, proj, clips_dir, log, on_update, stop_event, check_stop):
    finder = FootageFinder(site, settings, log)
    blocked = None
    for sr in results:
        check_stop()
        sr.status = "searching"
        if on_update: on_update(results)
        try:
            finder.pick(sr)
        except ps.Blocked as e:
            blocked = str(e); sr.status = "blocked"
            log(f"✗ {e} — stopping search." + ("" if settings.use_browser else
                " Turn on 'Use Chrome for Pexels' and try again."))
            break
        except Exception as e:  # noqa: BLE001
            if e.__class__.__name__ == "BrowserUnavailable":
                raise
            sr.status = f"error: {e}"
        log(f"[{sr.index:03d}/{len(results)}] {sr.status:9} '{sr.query_used or sr.queries[0]}' — {sr.text[:60]}")
        if on_update: on_update(results)

    # download in timeline order: 001a_..., 001b_...
    jobs = []
    for sr in results:
        for k, v in enumerate(sr.clips):
            letter = chr(ord("a") + k)
            v.keyword = "clips"
            fname = f"{sr.index:03d}{letter}_{slug(sr.query_used or v.title, 30)}_{v.id}"
            jobs.append((sr, v, fname))
    check_stop()
    log(f"Downloading {len(jobs)} clips …")
    dl = requests.Session(); dl.headers.update(ps.HEADERS)
    browser = hasattr(site, "download_via_browser")

    def _final_name(v, fname):
        return clips_dir / f"{fname}_{v.chosen_file.width}x{v.chosen_file.height}.mp4"

    def _one(job):
        sr, v, fname = job
        if stop_event is not None and stop_event.is_set():
            v.status = "skipped"; return
        ps.download(v, proj, dl, retries=2 if browser else 4)
        if v.status in ("downloaded", "exists"):
            final = _final_name(v, fname)
            Path(v.local_path).replace(final)
            v.local_path = str(final)
        if on_update: on_update(results)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, settings.workers)) as ex:
        list(ex.map(_one, jobs))

    # anything the direct download couldn't get → let Chrome download it (one at a time)
    failed = [j for j in jobs if j[1].status == "failed"]
    if failed and browser:
        log(f"Retrying {len(failed)} clips through Chrome …")
        for sr, v, fname in failed:
            check_stop()
            final = _final_name(v, fname)
            cf = v.chosen_file
            if site.download_via_browser(cf.download_link or cf.link, final, referer=v.pexels_url) or \
               (cf.download_link and site.download_via_browser(cf.link, final)):
                v.status, v.local_path = "downloaded", str(final)
                log(f"  ✓ {final.name}")
            if on_update: on_update(results)

    for leftover in clips_dir.glob("*.part"):
        try:
            leftover.unlink()
        except OSError:
            pass
    write_storyboard(proj, results)
    ok = sum(1 for _, v, _ in jobs if v.status in ("downloaded", "exists"))
    missing = [sr.index for sr in results if not sr.clips]
    log(f"Done: {ok}/{len(jobs)} clips downloaded, {len(missing)} sentences without footage → {proj}")
    return {"folder": str(proj), "sentences": len(results), "clips": ok,
            "missing": missing, "blocked": blocked}


def make_zip(folder: str | Path, dest: str | Path | None = None,
             progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Zip a project folder. Videos are stored without recompression (they're already compressed),
    so this is about as fast as copying the files."""
    import zipfile
    folder = Path(folder)
    dest = Path(dest) if dest else folder.with_suffix(".zip")
    files = [p for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.endswith(".part")]
    tmp = dest.with_name(dest.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
        for n, p in enumerate(files, 1):
            comp = zipfile.ZIP_STORED if p.suffix.lower() in (".mp4", ".mov", ".jpg", ".png", ".zip") \
                else zipfile.ZIP_DEFLATED
            z.write(p, Path(folder.name) / p.relative_to(folder), compress_type=comp)
            if progress:
                progress(n, len(files))
    os.replace(tmp, dest)
    return str(dest)


def write_storyboard(proj: Path, results: list[SentenceResult]):
    with open(proj / "storyboard.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "sentence", "est_seconds", "queries", "query_used", "clip_file", "clip_seconds",
                    "resolution", "pexels_url", "author"])
        for sr in results:
            if not sr.clips:
                w.writerow([sr.index, sr.text, sr.seconds, " | ".join(sr.queries), "", "NO MATCH", "", "", "", ""])
            for v in sr.clips:
                cf = v.chosen_file
                w.writerow([sr.index, sr.text, sr.seconds, " | ".join(sr.queries), sr.query_used,
                            Path(v.local_path).name if v.local_path else "", v.duration,
                            f"{cf.width}x{cf.height}" if cf else "", v.pexels_url, v.author_name])
    with open(proj / "CREDITS.txt", "w", encoding="utf-8") as fh:
        for sr in results:
            for v in sr.clips:
                fh.write(f"{v.title} — video by {v.author_name} on Pexels: {v.pexels_url}\n")
    rows = []
    for sr in results:
        thumbs = "".join(
            f'<a href="{html.escape(v.pexels_url)}" target="_blank"><img src="{html.escape(v.thumbnail)}" '
            f'loading="lazy"><span>{html.escape(Path(v.local_path).name if v.local_path else "")}</span></a>'
            for v in sr.clips) or '<em class="miss">No match — try your own search</em>'
        rows.append(f'<tr><td>{sr.index}</td><td>{html.escape(sr.text)}<div class="q">'
                    f'{html.escape(" · ".join(sr.queries))}</div></td><td class="t">{thumbs}</td></tr>')
    (proj / "storyboard.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>Storyboard — {html.escape(proj.name)}</title>
<style>body{{font:14px system-ui;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%}}
td{{border-bottom:1px solid #eee;padding:10px;vertical-align:top}}td:first-child{{color:#999;width:40px}}
.q{{color:#888;font-size:12px;margin-top:4px}}.t a{{display:inline-block;margin:0 8px 8px 0;font-size:11px;color:#555;text-decoration:none}}
.t img{{display:block;width:220px;border-radius:6px}}.miss{{color:#c33}}</style>
<h2>{html.escape(proj.name)}</h2><table>{''.join(rows)}</table>""", encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description="Find + download Pexels b-roll for every sentence of a script.")
    p.add_argument("script", help="script .txt file")
    p.add_argument("--name", help="project name (default: script file name)")
    p.add_argument("-o", "--out", default="broll_projects")
    p.add_argument("--deepseek-key", default=os.getenv("DEEPSEEK_API_KEY"))
    p.add_argument("--orientation", choices=ps.ORIENTATIONS, default="landscape")
    p.add_argument("--aspect", default="16:9", help="16:9, 9:16, 1:1 or 'any'")
    p.add_argument("--resolution", type=int, default=1080, choices=(720, 1080, 1440, 2160))
    p.add_argument("--clips", type=int, default=1, help="clips per sentence")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--exclude", nargs="+", default=[])
    p.add_argument("--no-browser", action="store_true", help="use plain HTTP instead of Chrome (often blocked)")
    p.add_argument("--site", default=ps.SITE, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    st = Settings(orientation=a.orientation,
                  aspect=None if a.aspect == "any" else ps.parse_aspect(a.aspect),
                  resolution=a.resolution, clips_per_sentence=a.clips, delay=a.delay, exclude=a.exclude,
                  use_browser=not a.no_browser)
    text = Path(a.script).read_text(encoding="utf-8")
    res = run_project(text, Path(a.out), a.name or Path(a.script).stem, st, a.deepseek_key, a.site)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
