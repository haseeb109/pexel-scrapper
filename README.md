# Pexels B-roll Finder

Paste a video script and get matching Pexels footage for every sentence, downloaded in timeline order. Includes a desktop app with an **Update** button.

## Install (first time)

1. On GitHub: **Code → Download ZIP**, then unzip it (e.g. into Downloads).
   *Or* clone it: `git clone https://github.com/haseeb109/pexel-scrapper.git`. With a clone, the Update button uses `git pull`.
2. **Mac only.** Clear the download block once, in Terminal (adjust the path to where you unzipped it):
   ```
   xattr -dr com.apple.quarantine ~/Downloads/pexel-scrapper-main
   chmod +x ~/Downloads/pexel-scrapper-main/*.command
   ```
3. Double-click **`Start (Mac).command`** or **`Start (Windows).bat`**. The first run sets itself up in 1–2 minutes.

## Updating

Click **⟳ Check for updates** at the top right of the app.
- The app also checks on startup. When a new version is on GitHub, the button turns into **⬆ Update available**.
- Updating downloads the latest code, keeps your `.venv`, projects, settings and DeepSeek key, installs any new packages, and restarts the app.

# Details

## ★★ Desktop app: double-click to open

| Mac | Windows |
|---|---|
| Double-click **`Start (Mac).command`** | Double-click **`Start (Windows).bat`** |

**First run:** the launcher sets itself up (about a minute) and opens the **Pexels B-roll Finder** window. Later launches open straight away. Packages are only reinstalled when `requirements.txt` changes.
- **Mac:** the launcher prefers python.org / Homebrew Python over Apple's old built-in Python 3.9. If you install a newer Python later, it rebuilds its environment by itself.

**Needs Python 3** with its window toolkit:
- Mac: the installer from python.org includes it. If you use Homebrew Python, run `brew install python-tk`.
- Windows: install from python.org and tick **Add python.exe to PATH**.

**Mac warning on first open:** macOS may say it "can't be opened because it is from an unidentified developer". Right-click the file → **Open** → **Open**. You only need to do this once.

**Chrome mode (on by default):** Pexels blocks plain Python requests with a bot-check, so the app opens a **separate Google Chrome window** with its own profile (`~/.pexels_broll_chrome`) and loads the Pexels search pages there.
- If Pexels shows **"Verify you are human" / "Just a moment…"**, complete it in that Chrome window. The app waits up to 5 minutes, then carries on by itself.
- The profile is kept between runs, so the check usually doesn't come back every time.
- Keep that Chrome window open while a job runs. It closes by itself when the job ends.
- If a clip can't be downloaded directly, Chrome downloads it, the same way the Download button on Pexels does.
- Google Chrome must be installed. If it isn't, the app downloads a browser once (~150 MB).

In the window:
1. Paste your **DeepSeek API key** (from platform.deepseek.com → API keys) and press **Test key**. Tick **Remember** so you never paste it again. The key is stored only on your computer, in `~/.pexels_broll.json`.
2. Enter a project name and choose the **Save to** folder.
3. Pick the format, quality and clips per sentence. Optionally add words to avoid.
4. Paste the script, or use **Load .txt…**. The counter under the box shows sentences, words and estimated narration time.
5. Press **▶ Find footage**. Every sentence shows its search query, clip count and status live. **■ Stop** cancels at any time.
6. When it's done, use one of the buttons:
   - **⬇ Download folder (ZIP)**: asks where to save, then builds the ZIP there. Videos are stored without recompression, so it's about as fast as copying.
   - **📂 Open folder**
   - **Storyboard**

   Double-click any row to play its clip.

## ★ Script → B-roll (new)

Paste a video script. The tool splits it into sentences, works out what each sentence should *show*, finds matching Pexels footage, and downloads everything in timeline order.

```bash
pip install requests flask
python app.py                       # opens http://127.0.0.1:5055 in your browser
```

1. Paste the script and give the project a name.
2. Pick the format (16:9 / 9:16 / 1:1), the quality (720p to 4K) and the number of clips per sentence.
3. Optional: paste a DeepSeek API key and tick **remember**.
4. Press **Find footage**. Each sentence shows up with its search queries and thumbnails as it's matched.
5. When it's done, use one of the buttons:
   - **⬇ Download folder (ZIP)**
   - **Open folder** (opens it in Finder / Explorer)
   - **Storyboard**

Project folder (`broll_projects/<name>/`):

```
clips/001a_courtroom_335574_1920x1080.mp4     ← number = sentence, letter = clip; sorts in timeline order
clips/001b_...
clips/002a_rain-window-night_...
storyboard.html    ← every sentence with its thumbnails (open in a browser)
storyboard.csv     ← sentence, est. seconds, queries, chosen clip, Pexels link, author
script.txt, CREDITS.txt
```

**How each sentence is matched**

- **Splitting:**
  - Cues like `[INTRO MUSIC]` are removed.
  - Fragments under 4 words (e.g. "Guilty.") are joined to the previous sentence.
- **Search queries:**
  - *With DeepSeek* (`deepseek-flash`, JSON output, 25 sentences per call): 3 queries per sentence, from most specific to broadest. Real names are turned into generic visuals ("Judge Judy" → "female judge courtroom"). It also gives an *avoid* list per sentence (e.g. "cartoon"). A whole script costs a fraction of a cent.
  - *Without DeepSeek*: built-in keyword extraction. It works, but the matches are noticeably weaker.
- **Clip length:** estimated as words ÷ 2.5 (about 150 wpm narration). The tool first looks for clips long enough to cover the line, then accepts any length.
- **Fallback:** if the first query finds nothing, the next one is tried.
- **No repeats:** the same clip is never used twice in a project.
- **Speed:** search results are cached, so repeated queries cost no extra requests.
- **Errors:**
  - A wrong DeepSeek key or no balance switches to keyword mode instead of failing the job.
  - If a single batch of 25 sentences fails, only that batch uses keywords.
- **Splitting:** abbreviations like "Mr.", "Dr." and "U.S." don't break sentences.
- **"Never use footage with":** whole words only, so "man" doesn't block "woman" or "Manhattan".
- **4K footage:** 4096×2160 counts as 16:9.

**Troubleshooting:** if the app hits an unexpected error, it shows a popup and writes details to `~/pexels_broll_error.log`. Send that file when reporting a problem.

**Command-line version (no UI)**

```bash
export DEEPSEEK_API_KEY=sk-...     # optional
python script_broll.py my_script.txt --name docket-ep-14 --clips 2 --resolution 1080
```

Speed: about 2 seconds per new search query (to stay polite to Pexels) plus download time. A 60-sentence script with DeepSeek takes about 3–6 minutes.

---

Pexels has stopped issuing new API keys, so this tool reads the public search pages directly:

```
https://www.pexels.com/search/videos/<keyword>/?page=N&orientation=landscape
```

Every search page embeds its results as JSON in `<script id="__NEXT_DATA__">`. That JSON includes every MP4 rendition with direct links. The tool parses it, filters and ranks the videos, and downloads the best HD/4K file.

## Setup

```bash
pip install requests
```

## Usage

```bash
# 5 Full-HD clips per keyword
python pexels_scraper.py "ocean waves" "city night traffic" -n 5

# YouTube b-roll: 16:9, 8–40 s, 1080p minimum, prefer ~15 s, skip clips with people
python pexels_scraper.py "courtroom" "judge gavel" -n 10 \
  --aspect 16:9 --min-duration 8 --max-duration 40 \
  --resolution 1080 --strict-resolution --target-duration 15 \
  --exclude people woman man

# Shorts / Reels: vertical 4K
python pexels_scraper.py "rain window" --orientation portrait --aspect 9:16 --resolution 2160

# Many keywords from a file, preview only (no downloads)
python pexels_scraper.py -k keywords.txt -n 20 --dry-run
```

Output (`pexels_downloads/`):

```
ocean-waves/1234567_ocean-waves-crashing-on-rocks_1920x1080.mp4
results.csv      ← every mapped field, one row per video (opens in Excel)
manifest.jsonl   ← same as JSON; used to resume
CREDITS.txt      ← videographer credits
```

Re-running into the same folder skips anything already downloaded. The same video is never downloaded twice, even if it appears under several keywords.

## Options

| Option | What it does |
|---|---|
| `keywords…` / `-k file.txt` | Search terms (`#` lines in the file are ignored) |
| `-n` | Videos per keyword (default 5) |
| `--orientation` | `landscape` / `portrait` / `square`. Sent to Pexels in the URL |
| `--max-pages` | Search pages per keyword, about 24 videos each (default 8) |
| `--delay` | Seconds between page requests, plus random jitter (default 2) |
| `--site-param K=V` | Extra URL parameters passed as-is (experimental, see the filter list below) |
| `--min/max-duration` | Seconds |
| `--min-width/--min-height` | Source pixels |
| `--min/max-fps` | e.g. `--min-fps 50` for slow-motion footage |
| `--aspect`, `--aspect-tol` | `16:9`, `9:16`, `1:1`, `2.39` |
| `--require WORD…` | Every word must appear in the title, description or tags |
| `--exclude WORD…` | Drop the video if any of these words appear there |
| `--exclude-author NAME…` | Skip specific videographers |
| `--resolution` | Target download size: `720`, `1080` (default), `1440`, `2160`. Measured on the short side, so a 1080×1920 vertical counts as 1080p |
| `--strict-resolution` | Skip a video if it isn't available at the target size or higher |
| `--target-duration` | Rank clips closer to this length higher |
| `--dry-run` | Scrape, rank and write the CSV without downloading |
| `--workers` | Parallel downloads (default 3) |

## How a video gets picked

1. **Scrape:** search pages are fetched one by one until there are twice as many matches as `-n`, or the results run out.
2. **Filter:** the rules above are applied, and duplicates are removed.
3. **Choose the file:**
   - the exact target size if it exists;
   - otherwise the next size up;
   - otherwise the best size below (unless `--strict-resolution`).

   Higher fps breaks ties. HLS streams are ignored.
4. **Score out of 100:**

   | Part | Points |
   |---|---|
   | Keyword match in title, description and tags | 40 |
   | Top available resolution (4K gets full marks) | 30 |
   | Fit to `--target-duration` (or 8–60 s if none is set) | 20 |
   | fps (60 gets full marks) | 10 |

   The top `-n` per keyword are downloaded.
5. **Download:** the file is streamed to `.part` and only renamed once its size checks out. An interrupted file resumes with an HTTP Range request. If the direct link fails, the tool falls back to `download_link`. Each file gets 4 retries.

## Site data map (captured from pexels.com in Chrome)

`__NEXT_DATA__ → props.pageProps.initialData`:

| Site field | CSV column |
|---|---|
| `data[].attributes.id` | `id` |
| `title` (falls back to the slug) | `title` |
| `description` | `description` |
| `slug` + `id` | `pexels_url` |
| `width`, `height`, `aspect_ratio` | `width`, `height`, `aspect_ratio`, `orientation` |
| `duration`, `fps` | `duration`, `fps` |
| `tags[].name` | `tags` |
| `user.first_name/last_name/username` | `author_name`, `author_url` |
| `video.thumbnail`, `video.preview_src` | `thumbnail`, `preview_url` |
| `video.video_files[]` `{quality, width, height, fps, link, download_link}` | `resolutions`, `max_resolution`, `chosen_file`, `download_url` |
| `created_at` | `created_at` |
| `pagination.total_pages/total_results` | used for paging |
| `meta.related_searches` | printed as keyword ideas |

Other things seen on the live site:

- `quality` can be `hd`, `sd`, `uhd` or `null`, so the tool chooses files by their pixel size, not by that label.
- The site's filter panel uses these keys: `orientation`, `people_count`, `people_age`, `duration`, `width`, `height`, `resolution_name`, `frame_rate`, `color`, `date_from`. Only `orientation` has been confirmed to work in the URL. You can try the others with `--site-param` (e.g. `--site-param people_count=0`). Duration, fps, size and aspect are always enforced by the tool itself either way.

## Notes

- **Bot check:** if Pexels shows its bot-check page ("Just a moment…" / 403), the tool stops cleanly and says so. Wait a while, raise `--delay`, or try another network. The tool does not try to get around the check.
- **Pace:** keep `--delay` at 2 s or more. Downloads come from the video CDN and don't count against search pages.
- **License:** Pexels videos are free to use, including commercially. Crediting the videographer is appreciated, which is what `CREDITS.txt` is for.
- **Fragility:** if Pexels changes its page layout, the tool will report `__NEXT_DATA__ not found` instead of silently saving bad data.

## Test

```bash
python tests/test_e2e.py     # full flow against a local mock of the site
```
