# Max Media Downloader

A Python Flask web application that provides a browser-based interface for downloading **openly licensed** video and audio (Wikimedia Commons, Internet Archive CC / public-domain items, or domains you own) using **yt-dlp**. YouTube, X and other platforms whose terms forbid downloading are refused.

## Features

- Browser-based user interface
- Video and audio downloads
- Multiple output formats
- Download progress tracking
- File management
- yt-dlp integration
- Flask backend

## Technologies

- Python
- Flask
- HTML
- CSS
- JavaScript
- yt-dlp
- FFmpeg

---

## Installation

```bash
pip install -r requirements.txt
```

Install **FFmpeg** (required for audio extraction and format conversion):

- **Windows:** `winget install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`
## 2. Run the server

```bash
python app.py
```

Open **http://localhost:5000** — the frontend (`index.html`) is served automatically
from the `static/` folder, and it talks to the API on the same origin.

For production, use a real WSGI server instead of the dev server:

```bash
gunicorn app:app --workers 1 --threads 8 --timeout 0 -b 0.0.0.0:5000
```

## 3. How it works

| Endpoint                  | Method | Purpose                                              |
|----------------------------|--------|-------------------------------------------------------|
| `/api/health`              | GET    | Check yt-dlp / ffmpeg availability                    |
| `/api/analyze`              | POST   | `{ "url": "..." }` → metadata + list of real streams  |
| `/api/download`             | POST   | Starts a background download job, returns `job_id`    |
| `/api/status/<job_id>`      | GET    | Poll job progress (fallback if SSE unsupported)        |
| `/api/progress/<job_id>`    | GET    | Server-Sent Events stream of live progress             |
| `/api/file/<job_id>`        | GET    | Downloads the finished file once `status == "done"`    |

**Flow:** paste URL → frontend calls `/api/analyze` (yt-dlp probes the real
formats available) → user clicks a stream → frontend calls `/api/download`
(spawns a background thread running yt-dlp, optionally with ffmpeg
post-processing for audio) → frontend subscribes to `/api/progress/<id>` for
live % → on completion, the browser auto-downloads from `/api/file/<id>`.

Files are written to a temp directory and **auto-deleted 5 minutes** after
completion to avoid disk buildup.

## 4. Compliant mode (sources policy)

The server only downloads media it is allowed to redistribute:

| Source | Rule |
|---|---|
| Wikimedia Commons (`commons.wikimedia.org`, `upload.wikimedia.org`) | Allowed; Commons only hosts freely licensed media. |
| Internet Archive (`archive.org`) | Allowed only when the item's `licenseurl` is Creative Commons or public domain (checked via `archive.org/metadata/<id>`). Set `REQUIRE_OPEN_LICENSE=0` to skip the check for items you own. |
| Your own domains | Add them to `ALLOWED_DOMAINS` (comma separated, subdomains included). |
| YouTube, X/Twitter, Instagram, TikTok, Facebook | Always refused with a message explaining why (their Terms of Service prohibit downloading). They cannot be re-enabled via `ALLOWED_DOMAINS`. |
| Everything else | Refused: "not on the allowed list". |

Safety checks applied to the pasted URL **and** every media URL yt-dlp
resolves from it: http(s) only, ports 80/443 only, no raw IPs, no embedded
credentials, and the host must resolve to a public address (blocks SSRF to
localhost, cloud metadata or private networks). Playlists/collections are
refused (paste a single file). Client-supplied format ids and options are
validated against fixed lists, so nothing user-controlled reaches yt-dlp's
format language or ffmpeg arguments.

Limits: `MAX_FILESIZE_MB` (default 500, enforced before and during download),
`MAX_ACTIVE_JOBS` (default 2, extra requests get HTTP 429),
`MAX_DURATION_SECONDS` (default 3 h), `JOB_TTL_SECONDS` (default 300, files
and job records are deleted after this; a janitor runs every minute and the
temp dir is wiped on start).

Cookie support for login-walled sites was removed: those sites are not allowed sources.

## 5. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # offline unit tests
LIVE=1 python -m pytest -q tests/test_live.py   # real downloads from Commons / archive.org
```

`SSRF_ALLOW_NETS` exists only for sandboxes whose DNS returns synthetic
addresses (e.g. `198.18.0.0/15` behind a fake-IP proxy). Never set it in production.

## Deploy on Render (free)

This repo includes a `Dockerfile` (Python slim + FFmpeg + gunicorn) and a `render.yaml` blueprint.

1. In Render: **New > Blueprint**, connect this GitHub repo, and apply `render.yaml`
   (or **New > Web Service**, pick this repo, Runtime **Docker**, plan **Free**, health check path `/api/health`).
2. Optional env vars: `MAX_FILESIZE_MB` (default 500, `0` = no limit), `JOB_TTL_SECONDS` (default 300), `MAX_ACTIVE_JOBS` (default 2), `ALLOWED_DOMAINS`. If you previously set `COOKIES_CONTENT` on Render, delete it: it is no longer used.

The server runs a single gunicorn worker with threads, because download jobs are tracked in memory.
Free instances sleep when idle and have ephemeral disk, so finished files are only kept for a few minutes.

Only download content you have the rights to; downloading from some platforms may violate their Terms of Service.
