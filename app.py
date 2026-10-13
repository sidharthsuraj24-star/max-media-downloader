"""
Max Media Downloader — Flask Backend
=====================================
Requires:  pip install flask flask-cors yt-dlp
Optional:  ffmpeg  (for audio extraction & conversion)

Run:
    python app.py
    # or for production:
    gunicorn -w 4 -b 0.0.0.0:5000 app:app
"""

import os
import re
import uuid
import json
import shutil
import tempfile
import threading
import subprocess
import mimetypes
from urllib.parse import urlparse
from flask import (
    Flask, request, jsonify, send_file,
    Response, stream_with_context
)

# ── Try importing optional dependencies ───────────────────────────────────────
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False
    print("[WARN] yt-dlp not installed.  Run: pip install yt-dlp")

try:
    from flask_cors import CORS
    CORS_AVAILABLE = True
except ImportError:
    CORS_AVAILABLE = False
    print("[WARN] flask-cors not installed.  Run: pip install flask-cors")

# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="")

if CORS_AVAILABLE:
    CORS(app, resources={r"/api/*": {"origins": "*"}})
else:
    # Minimal CORS headers without the library
    @app.after_request
    def add_cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

# ── Config ───────────────────────────────────────────────────────────────────
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "max_media_downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
# Upper bound per download (MB); 0 disables. Keeps small hosts from filling disk.
MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", "0") or 0)
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "300") or 300)

# Track active jobs:  job_id -> {status, progress, filename, error, filepath}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def clean_error_message(exc) -> str:
    """Return a concise message suitable for API clients."""
    msg = str(exc) or "Download failed."
    msg = msg.replace("ERROR: ", "")
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg or "Download failed."


def sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe in filenames."""
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    return name[:200]


def job_dir(job_id: str) -> str:
    d = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def cleanup_job(job_id: str, delay: int = 300):
    """Delete job files after `delay` seconds."""
    def _del():
        import time
        time.sleep(delay)
        d = os.path.join(DOWNLOAD_DIR, job_id)
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
    threading.Thread(target=_del, daemon=True).start()


# ── yt-dlp progress hook ──────────────────────────────────────────────────────

def make_progress_hook(job_id: str):
    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            status = d.get("status")
            if status == "downloading":
                job["status"] = "downloading"
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                pct = int(downloaded / total * 90) if total else 0
                job["progress"] = pct
                job["speed"] = d.get("_speed_str", "")
                job["eta"] = d.get("_eta_str", "")
            elif status == "finished":
                job["progress"] = 95
                job["status"] = "processing"
    return hook


# ── yt-dlp format selector ────────────────────────────────────────────────────

def build_ydl_opts(dl_type: str, quality: str, fmt: str,
                   audio_fmt: str, audio_quality: str,
                   out_dir: str, job_id: str,
                   format_id: str = "", has_audio: bool = False) -> dict:
    """
    Returns a yt-dlp options dict for the requested download type.
    dl_type  : 'video' | 'audio' | 'convert'
    quality  : 'best' | '1080p' | '720p' | '480p' | '360p' | '144p'
    fmt      : 'mp4'  | 'webm'  | 'mkv'  | 'any'
    audio_fmt: 'mp3'  | 'aac'   | 'flac' | 'wav' | 'ogg' | 'm4a' | 'opus'
    audio_quality: '320' | '256' | '192' | '128' | '64'
    """
    outtmpl = os.path.join(out_dir, "%(title)s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [make_progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if MAX_FILESIZE_MB > 0:
        opts["max_filesize"] = MAX_FILESIZE_MB * 1024 * 1024

    if format_id and dl_type == "video":
        container = fmt if fmt else "mp4"
        if has_audio:
            opts["format"] = format_id
        else:
            opts["format"] = f"{format_id}+bestaudio/best"
        if container in ("mp4", "mkv", "webm"):
            opts["merge_output_format"] = container
    elif format_id and dl_type == "audio":
        opts["format"] = format_id
    elif dl_type in ("audio", "convert"):
        # Extract best audio and transcode
        afmt = audio_fmt if audio_fmt else "mp3"
        abr  = audio_quality if audio_quality else "192"
        # Prefer audio-only; otherwise a small muxed stream instead of the
        # largest one (avoids pulling multi-GB 4K files just for audio).
        opts["format"] = "bestaudio/best[height<=480]/worst[acodec!=none]/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": afmt,
            "preferredquality": abr,
        }]
    else:
        # Video download
        height_map = {
            "1080p": 1080, "720p": 720, "480p": 480,
            "360p": 360, "144p": 144, "best": None,
        }
        height = height_map.get(quality)
        container = fmt if fmt else "mp4"

        if height:
            vformat = (
                f"bestvideo[height<={height}][ext={container}]+"
                f"bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]/"
                "best"
            )
        else:
            vformat = (
                f"bestvideo[ext={container}]+bestaudio[ext=m4a]/"
                "bestvideo+bestaudio/best"
            )

        opts["format"] = vformat

        # Merge into requested container
        if container in ("mp4", "mkv", "webm"):
            opts["merge_output_format"] = container

    return opts


# ── Background download worker ────────────────────────────────────────────────

def download_worker(job_id: str, url: str, opts: dict):
    out_dir = job_dir(job_id)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Find the downloaded file
        files = [f for f in os.listdir(out_dir)
                 if not f.endswith(".part") and not f.endswith(".ytdl")]
        if not files:
            raise RuntimeError("Download completed but no output file found.")

        # Pick the largest finished file (the final output, not leftovers)
        files.sort(key=lambda f: os.path.getsize(os.path.join(out_dir, f)), reverse=True)
        filepath = os.path.join(out_dir, files[0])
        filename = files[0]

        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done",
                "progress": 100,
                "filename": filename,
                "filepath": filepath,
            })

        # Auto-clean after 5 minutes
        cleanup_job(job_id, delay=JOB_TTL_SECONDS)

    except Exception as exc:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id].update({
                    "status": "error",
                    "error": clean_error_message(exc),
                })
        # Remove partial files now; drop the job record after the TTL
        shutil.rmtree(out_dir, ignore_errors=True)
        cleanup_job(job_id, delay=JOB_TTL_SECONDS)


# ════════════════════════════════════════════════════════════════════════════
#  API ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "yt_dlp": YT_DLP_AVAILABLE,
        "ffmpeg": ffmpeg_available(),
    })


# ── 1. Analyze URL → return metadata & available streams ─────────────────────
@app.route("/api/analyze", methods=["POST", "OPTIONS"])
def analyze():
    if request.method == "OPTIONS":
        return "", 204

    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp is not installed on the server."}), 503

    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL."}), 400

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Build a clean stream list from the raw formats
        streams = _parse_streams(info)

        return jsonify({
            "title":      info.get("title", "Unknown Title"),
            "uploader":   info.get("uploader", ""),
            "duration":   _fmt_duration(info.get("duration")),
            "thumbnail":  info.get("thumbnail", ""),
            "platform":   info.get("extractor_key", "Web"),
            "webpage_url":info.get("webpage_url", url),
            "streams":    streams,
        })

    except yt_dlp.utils.DownloadError as exc:
        return jsonify({"error": clean_error_message(exc)}), 422
    except Exception as exc:
        return jsonify({"error": f"Unexpected error: {clean_error_message(exc)}"}), 500


def _fmt_duration(secs):
    if not secs:
        return "—"
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_streams(info: dict) -> list[dict]:
    """Convert yt-dlp format list → simplified stream objects for the UI."""
    formats = info.get("formats") or []
    seen = set()
    streams = []

    # ── Video streams ──────────────────────────────────────────────────────
    VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
    for h in VIDEO_HEIGHTS:
        for ext in ("mp4", "webm"):
            candidates = [
                f for f in formats
                if f.get("vcodec", "none") not in ("none", None, "")
                and f.get("height") == h
                and f.get("ext") == ext
            ]
            if not candidates:
                continue
            best = max(candidates, key=lambda f: f.get("tbr") or 0)
            key = (h, ext)
            if key in seen:
                continue
            seen.add(key)
            size_mb = _size_str(best)
            label = f"{ext.upper()} {h}p {'4K' if h==2160 else 'QHD' if h==1440 else 'HD' if h>=720 else ''}"
            streams.append({
                "type":    "video",
                "label":   label.strip(),
                "quality": f"{h}p",
                "fmt":     ext,
                "size":    size_mb,
                "format_id": best.get("format_id", ""),
                "has_audio": bool(best.get("acodec") and best["acodec"] != "none"),
            })

    # ── Audio-only streams ─────────────────────────────────────────────────
    AUDIO_FMTS = ["mp3", "m4a", "aac", "opus", "ogg", "webm"]
    for ext in AUDIO_FMTS:
        candidates = [
            f for f in formats
            if f.get("vcodec", "none") in ("none", None, "")
            and f.get("acodec", "none") not in ("none", None, "")
            and f.get("ext") == ext
        ]
        if not candidates:
            continue
        best = max(candidates, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        abr = best.get("abr") or best.get("tbr") or 0
        label = f"{ext.upper()} {int(abr)}kbps Audio" if abr else f"{ext.upper()} Audio"
        streams.append({
            "type":    "audio",
            "label":   label,
            "quality": f"{int(abr)}kbps" if abr else "best",
            "fmt":     ext,
            "size":    _size_str(best),
            "format_id": best.get("format_id", ""),
            "has_audio": True,
        })

    # ── Conversion targets (always offered if ffmpeg present) ─────────────
    if ffmpeg_available():
        for afmt, label, bitrate in [
            ("mp3",  "MP3 320kbps (converted)",  "320"),
            ("aac",  "AAC 256kbps (converted)",  "256"),
            ("flac", "FLAC Lossless (converted)", "0"),
            ("wav",  "WAV Lossless (converted)",  "0"),
            ("m4a",  "M4A 256kbps (converted)",  "256"),
        ]:
            streams.append({
                "type":    "convert",
                "label":   label,
                "quality": bitrate + "kbps" if bitrate != "0" else "lossless",
                "fmt":     afmt,
                "size":    "~varies",
                "format_id": "bestaudio",
                "has_audio": True,
            })

    return streams


def _size_str(fmt: dict) -> str:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        mb = size / 1_048_576
        return f"~{mb:.0f} MB" if mb >= 1 else f"~{size/1024:.0f} KB"
    return "?"


# ── 2. Start a download job ───────────────────────────────────────────────────
@app.route("/api/download", methods=["POST", "OPTIONS"])
def start_download():
    if request.method == "OPTIONS":
        return "", 204

    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp is not installed on the server."}), 503

    data = request.get_json(silent=True) or {}
    url           = (data.get("url") or "").strip()
    dl_type       = data.get("type", "video")          # video | audio | convert
    quality       = data.get("quality", "best")        # best | 1080p | 720p …
    fmt           = data.get("format", "mp4")          # mp4 | webm | mkv …
    audio_fmt     = data.get("audio_format", "mp3")    # mp3 | aac | flac …
    audio_quality = data.get("audio_quality", "192")   # 320 | 256 | 192 …
    format_id     = (data.get("format_id") or "").strip()
    has_audio     = bool(data.get("has_audio"))

    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL."}), 400

    job_id  = str(uuid.uuid4())
    out_dir = job_dir(job_id)

    opts = build_ydl_opts(
        dl_type, quality, fmt,
        audio_fmt, audio_quality,
        out_dir, job_id,
        format_id, has_audio
    )

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status":   "queued",
            "progress": 0,
            "filename": None,
            "filepath": None,
            "error":    None,
            "speed":    "",
            "eta":      "",
        }

    t = threading.Thread(
        target=download_worker,
        args=(job_id, url, opts),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


# ── 3. Poll job status ────────────────────────────────────────────────────────
@app.route("/api/status/<job_id>")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify({
        "status":   job["status"],
        "progress": job["progress"],
        "filename": job["filename"],
        "error":    job["error"],
        "speed":    job.get("speed", ""),
        "eta":      job.get("eta", ""),
    })


# ── 4. Download the completed file ───────────────────────────────────────────
@app.route("/api/file/<job_id>")
def serve_file(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] != "done":
        return jsonify({"error": "File not ready yet."}), 202
    if not job["filepath"] or not os.path.exists(job["filepath"]):
        return jsonify({"error": "File missing on server."}), 410

    mime, _ = mimetypes.guess_type(job["filepath"])
    mime = mime or "application/octet-stream"

    return send_file(
        job["filepath"],
        mimetype=mime,
        as_attachment=True,
        download_name=job["filename"],
    )


# ── 5. SSE: real-time progress stream ────────────────────────────────────────
@app.route("/api/progress/<job_id>")
def progress_stream(job_id: str):
    """Server-Sent Events endpoint — browser EventSource() can subscribe here."""
    def generate():
        import time
        while True:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                yield "data: {\"error\": \"not_found\"}\n\n"
                break
            payload = {
                "status":   job["status"],
                "progress": job["progress"],
                "speed":    job.get("speed", ""),
                "eta":      job.get("eta", ""),
                "filename": job.get("filename"),
                "error":    job.get("error"),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Serve the frontend ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return app.send_static_file("index.html")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  Max Media Downloader — Backend")
    print(f"  yt-dlp   : {'✓ ready' if YT_DLP_AVAILABLE else '✗ NOT installed'}")
    print(f"  ffmpeg   : {'✓ found' if ffmpeg_available() else '✗ not found (audio conversion limited)'}")
    print(f"  temp dir : {DOWNLOAD_DIR}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
