# Max Media Downloader

A Python Flask web application that provides a browser-based interface for downloading video and audio from supported platforms using **yt-dlp**.

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
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
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

## 4. Notes / limitations

- Some sites (Instagram, TikTok, Facebook) require login cookies for private
  or rate-limited content — pass `cookiefile` in `build_ydl_opts()` if needed.
- yt-dlp is updated frequently as sites change; keep it current with
  `pip install -U yt-dlp`.
- This tool is for personal use on content you have rights to download —
  respect copyright and each platform's terms of service.

## Deploy on Render (free)

This repo includes a `Dockerfile` (Python slim + FFmpeg + gunicorn) and a `render.yaml` blueprint.

1. In Render: **New > Blueprint**, connect this GitHub repo, and apply `render.yaml`
   (or **New > Web Service**, pick this repo, Runtime **Docker**, plan **Free**, health check path `/api/health`).
2. Optional env vars: `MAX_FILESIZE_MB` (default 500, `0` = no limit), `JOB_TTL_SECONDS` (default 300).

The server runs a single gunicorn worker with threads, because download jobs are tracked in memory.
Free instances sleep when idle and have ephemeral disk, so finished files are only kept for a few minutes.

Only download content you have the rights to; downloading from some platforms may violate their Terms of Service.
