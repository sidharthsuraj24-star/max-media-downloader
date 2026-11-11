"""
Max Media Downloader — Flask backend (compliant mode)
=====================================================
Downloads openly licensed media (Wikimedia Commons, Internet Archive items
with an open licence, plus operator-owned domains in ALLOWED_DOMAINS) using
yt-dlp, with optional ffmpeg audio conversion. YouTube, X and other platforms
whose terms forbid downloading are refused. See policy.py.

Run locally:   python app.py
Production:    gunicorn app:app --workers 1 --threads 8 --bind 0.0.0.0:$PORT
(one worker: the job table lives in memory)
"""

import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

import policy
from policy import PolicyError

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:  # pragma: no cover
    yt_dlp = None
    YT_DLP_AVAILABLE = False
    print("[WARN] yt-dlp not installed.  Run: pip install yt-dlp")

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # JSON bodies only


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR") or os.path.join(
    tempfile.gettempdir(), "max_media_downloads")
# Leftovers from a previous process are orphaned (jobs live in memory).
shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_FILESIZE_MB = _int_env("MAX_FILESIZE_MB", 500)          # 0 = no limit
JOB_TTL_SECONDS = _int_env("JOB_TTL_SECONDS", 300)
MAX_ACTIVE_JOBS = _int_env("MAX_ACTIVE_JOBS", 2)
MAX_DURATION_SECONDS = _int_env("MAX_DURATION_SECONDS", 3 * 3600)  # 0 = no limit
SSE_MAX_SECONDS = _int_env("SSE_MAX_SECONDS", 3600)

DL_TYPES = {"video", "audio", "convert"}
QUALITIES = {"best": None, "1080p": 1080, "720p": 720, "480p": 480,
             "360p": 360, "240p": 240, "144p": 144}
CONTAINERS = {"mp4", "webm", "mkv"}
AUDIO_FORMATS = {"mp3", "aac", "flac", "wav", "ogg", "m4a", "opus"}
AUDIO_QUALITIES = {"320", "256", "192", "128", "64", "0"}
FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
JOB_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
AUDIO_EXTS = {"mp3", "m4a", "aac", "opus", "ogg", "oga", "flac", "wav", "weba"}
VIDEO_EXTS = {"mp4", "webm", "ogv", "mov", "mkv", "m4v", "mpeg", "avi"}

# job_id -> {status, progress, filename, filepath, error, speed, eta, expires}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


class TooLarge(Exception):
    pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def clean_error_message(exc) -> str:
    msg = str(exc) or "Download failed."
    msg = msg.replace("ERROR: ", "")
    msg = re.sub(r"\s*\(caused by .*", "", msg)
    msg = msg.replace(DOWNLOAD_DIR, "")
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:300] or "Download failed."


def limit_bytes():
    return MAX_FILESIZE_MB * 1024 * 1024 if MAX_FILESIZE_MB > 0 else 0


def base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "cachedir": False,
        # never read local files or follow file:// URLs
        "enable_file_urls": False,
    }


def purge_expired():
    now = time.time()
    with JOBS_LOCK:
        dead = [j for j, v in JOBS.items() if v.get("expires") and v["expires"] <= now]
        for j in dead:
            JOBS.pop(j, None)
    for j in dead:
        shutil.rmtree(os.path.join(DOWNLOAD_DIR, j), ignore_errors=True)


def _janitor(interval=60):
    while True:
        time.sleep(interval)
        try:
            purge_expired()
        except Exception:  # noqa: BLE001 - keep the janitor alive
            pass


threading.Thread(target=_janitor, daemon=True, name="janitor").start()


def active_jobs():
    with JOBS_LOCK:
        return sum(1 for v in JOBS.values()
                   if v["status"] in ("queued", "downloading", "processing"))


def format_urls(fmt: dict):
    if fmt.get("url"):
        yield fmt["url"]
    for f in fmt.get("fragments") or []:
        if f.get("url"):
            yield f["url"]
    if fmt.get("fragment_base_url"):
        yield fmt["fragment_base_url"]
    if fmt.get("manifest_url"):
        yield fmt["manifest_url"]


def format_allowed(fmt: dict) -> bool:
    try:
        for u in format_urls(fmt):
            policy.check_url(u)
        return True
    except PolicyError:
        return False


def probe(url: str, opts: dict | None = None):
    """Validate the URL, check its licence, and extract (unprocessed) info."""
    parsed = policy.check_url(url)
    licence = policy.check_license(parsed)
    o = base_opts()
    o.update(opts or {})
    o["skip_download"] = True
    with yt_dlp.YoutubeDL(o) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    if info.get("_type") in ("playlist", "multi_video"):
        raise PolicyError("That link is a collection. Open a single file's page and paste that link.")
    if info.get("_type") == "url":
        policy.check_url(info.get("url", ""))
        raise PolicyError("That page links elsewhere. Paste the direct media page instead.")
    for key in ("webpage_url",):
        if info.get(key):
            policy.check_url(info[key])
    # Drop any format whose media lives outside the allowlist.
    info["formats"] = [f for f in (info.get("formats") or []) if format_allowed(f)]
    if not info["formats"]:
        if info.get("url") and format_allowed(info):
            pass
        else:
            raise PolicyError("No downloadable media from an allowed source was found at that link.")
    dur = info.get("duration") or 0
    if MAX_DURATION_SECONDS and dur and dur > MAX_DURATION_SECONDS:
        raise PolicyError(f"Media longer than {MAX_DURATION_SECONDS // 3600} h is not supported here.")
    return info, licence


# ── Progress hook ────────────────────────────────────────────────────────────

def make_progress_hook(job_id: str):
    cap = limit_bytes()

    def hook(d):
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if cap and (downloaded > cap or (d.get("total_bytes") or 0) > cap):
            raise TooLarge(f"File is larger than the {MAX_FILESIZE_MB} MB limit.")
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            status = d.get("status")
            if status == "downloading":
                job["status"] = "downloading"
                job["progress"] = min(90, int(downloaded / total * 90)) if total else job["progress"]
                job["speed"] = d.get("_speed_str", "")
                job["eta"] = d.get("_eta_str", "")
            elif status == "finished":
                job["progress"] = 95
                job["status"] = "processing"
    return hook


# ── Format selection ─────────────────────────────────────────────────────────

def build_ydl_opts(dl_type: str, quality: str, fmt: str,
                   audio_fmt: str, audio_quality: str,
                   out_dir: str, job_id: str,
                   format_id: str = "", has_audio: bool = False) -> dict:
    """yt-dlp options for a download. All inputs must already be validated."""
    opts = base_opts()
    opts.update({
        "outtmpl": os.path.join(out_dir, "%(title).150B.%(ext)s"),
        "progress_hooks": [make_progress_hook(job_id)],
        "windowsfilenames": True,
    })
    if limit_bytes():
        opts["max_filesize"] = limit_bytes()

    if dl_type == "convert":
        opts["format"] = "bestaudio/best[height<=480]/worst/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_fmt or "mp3",
            "preferredquality": audio_quality or "192",
        }]
    elif format_id:
        if dl_type == "video" and not has_audio:
            opts["format"] = f"{format_id}+bestaudio/{format_id}"
        else:
            opts["format"] = format_id
        if dl_type == "video" and fmt in CONTAINERS:
            opts["merge_output_format"] = fmt
    elif dl_type == "audio":
        opts["format"] = "bestaudio/best"
    else:
        height = QUALITIES.get(quality)
        container = fmt if fmt in CONTAINERS else "mp4"
        h = f"[height<={height}]" if height else ""
        opts["format"] = (f"bestvideo{h}[ext={container}]+bestaudio/"
                          f"bestvideo{h}+bestaudio/best{h}/best")
        opts["merge_output_format"] = container
    return opts


# ── Background worker ────────────────────────────────────────────────────────

def _finish(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)
            JOBS[job_id]["expires"] = time.time() + JOB_TTL_SECONDS


def download_worker(job_id: str, url: str, opts: dict):
    out_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)
    try:
        info, _ = probe(url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            processed = ydl.process_ie_result(dict(info), download=False)
            selected = processed.get("requested_formats") or [processed]
            cap = limit_bytes()
            total = 0
            for f in selected:
                if not format_allowed(f):
                    raise PolicyError("The selected media is hosted outside the allowed sources.")
                total += f.get("filesize") or f.get("filesize_approx") or 0
            if cap and total > cap:
                raise TooLarge(f"File is larger than the {MAX_FILESIZE_MB} MB limit.")
            if cap and shutil.disk_usage(DOWNLOAD_DIR).free < cap * 2:
                raise RuntimeError("Server is low on disk space. Try again later.")
            ydl.process_ie_result(processed, download=True)

        files = [f for f in os.listdir(out_dir)
                 if not f.endswith((".part", ".ytdl", ".temp")) and not f.startswith(".")]
        if not files:
            raise RuntimeError("Download finished but no output file was produced"
                               + (f" (over the {MAX_FILESIZE_MB} MB limit?)." if limit_bytes() else "."))
        files.sort(key=lambda f: os.path.getsize(os.path.join(out_dir, f)), reverse=True)
        _finish(job_id, status="done", progress=100, filename=files[0],
                filepath=os.path.join(out_dir, files[0]))
    except Exception as exc:  # noqa: BLE001 - reported to the user
        shutil.rmtree(out_dir, ignore_errors=True)
        _finish(job_id, status="error", error=clean_error_message(exc))


# ════════════════════════════════════════════════════════════════════════════
#  API
# ════════════════════════════════════════════════════════════════════════════

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' https: data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
    return resp


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "yt_dlp": YT_DLP_AVAILABLE,
        "ffmpeg": ffmpeg_available(),
        "mode": "compliant",
        "allowed_domains": list(policy.allowed_domains()),
        "max_filesize_mb": MAX_FILESIZE_MB,
    })


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp is not installed on the server."}), 503
    url = str(_json_body().get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    try:
        info, licence = probe(url)
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = ydl.process_ie_result(info, download=False)
    except PolicyError as exc:
        return jsonify({"error": str(exc), "blocked": True}), 403
    except yt_dlp.utils.DownloadError as exc:
        return jsonify({"error": clean_error_message(exc)}), 422
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Unexpected error: {clean_error_message(exc)}"}), 500

    thumb = info.get("thumbnail") or ""
    try:
        policy.check_url(thumb, resolve=False)
    except PolicyError:
        thumb = ""
    return jsonify({
        "title": info.get("title") or "Untitled",
        # archive.org reports the uploader's e-mail address; don't echo it
        "uploader": "" if "@" in str(info.get("uploader") or "") else (info.get("uploader") or ""),
        "duration": _fmt_duration(info.get("duration")),
        "thumbnail": thumb,
        "platform": info.get("extractor_key") or "Web",
        "license": licence,
        "webpage_url": info.get("webpage_url") or url,
        "streams": _parse_streams(info),
    })


def _fmt_duration(secs):
    if not secs:
        return "—"
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _kind(f: dict) -> str | None:
    vcodec, acodec = f.get("vcodec"), f.get("acodec")
    ext = (f.get("ext") or "").lower()
    if vcodec and vcodec != "none":
        return "video"
    if f.get("height") or f.get("width"):
        return "video"
    if vcodec == "none" and acodec and acodec != "none":
        return "audio"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def _parse_streams(info: dict) -> list[dict]:
    """Every real format yt-dlp found, simplified for the UI."""
    formats = info.get("formats") or ([info] if info.get("url") else [])
    videos, audios = [], []
    for f in formats:
        fid = str(f.get("format_id") or "")
        if not FORMAT_ID_RE.match(fid) or f.get("protocol") in ("mhtml",):
            continue
        kind = _kind(f)
        ext = (f.get("ext") or "").lower()
        if kind == "video":
            h = f.get("height") or 0
            tag = " 4K" if h >= 2160 else " QHD" if h >= 1440 else " HD" if h >= 720 else ""
            videos.append({
                "type": "video",
                "label": f"{ext.upper()} {h}p{tag}" if h else f"{ext.upper()} video",
                "quality": f"{h}p" if h else "best",
                "fmt": ext,
                "size": _size_str(f),
                "format_id": fid,
                # unknown acodec: assume the file already carries its audio
                "has_audio": f.get("acodec") != "none",
                "_h": h, "_tbr": f.get("tbr") or 0,
            })
        elif kind == "audio":
            abr = f.get("abr") or f.get("tbr") or 0
            audios.append({
                "type": "audio",
                "label": f"{ext.upper()} {int(abr)}kbps Audio" if abr else f"{ext.upper()} Audio",
                "quality": f"{int(abr)}kbps" if abr else "best",
                "fmt": ext,
                "size": _size_str(f),
                "format_id": fid,
                "has_audio": True,
                "_h": 0, "_tbr": abr,
            })
    videos.sort(key=lambda s: (s["_h"], s["_tbr"]), reverse=True)
    audios.sort(key=lambda s: s["_tbr"], reverse=True)
    streams = videos + audios
    for s in streams:
        s.pop("_h"), s.pop("_tbr")

    if ffmpeg_available() and streams:
        for afmt, label, bitrate in [
            ("mp3", "MP3 320kbps (converted)", "320"),
            ("aac", "AAC 256kbps (converted)", "256"),
            ("flac", "FLAC Lossless (converted)", "0"),
            ("wav", "WAV Lossless (converted)", "0"),
            ("m4a", "M4A 256kbps (converted)", "256"),
        ]:
            streams.append({
                "type": "convert", "label": label,
                "quality": bitrate + "kbps" if bitrate != "0" else "lossless",
                "fmt": afmt, "size": "~varies", "format_id": "", "has_audio": True,
            })
    return streams


def _size_str(fmt: dict) -> str:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        mb = size / 1_048_576
        return f"~{mb:.0f} MB" if mb >= 1 else f"~{size / 1024:.0f} KB"
    return "?"


@app.route("/api/download", methods=["POST"])
def start_download():
    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp is not installed on the server."}), 503
    purge_expired()
    data = _json_body()
    url = str(data.get("url") or "").strip()
    dl_type = str(data.get("type") or "video")
    quality = str(data.get("quality") or "best")
    fmt = str(data.get("format") or "mp4").lower()
    audio_fmt = str(data.get("audio_format") or "mp3").lower()
    audio_quality = str(data.get("audio_quality") or "192").replace("kbps", "")
    if audio_quality == "lossless":
        audio_quality = "0"
    format_id = str(data.get("format_id") or "").strip()
    has_audio = bool(data.get("has_audio"))

    if not url:
        return jsonify({"error": "No URL provided."}), 400
    if dl_type not in DL_TYPES:
        return jsonify({"error": "Invalid download type."}), 400
    if format_id and not FORMAT_ID_RE.match(format_id):
        return jsonify({"error": "Invalid format id."}), 400
    if quality not in QUALITIES:
        quality = "best"
    if dl_type == "convert":
        if audio_fmt not in AUDIO_FORMATS or audio_quality not in AUDIO_QUALITIES:
            return jsonify({"error": "Invalid audio format or bitrate."}), 400
        if not ffmpeg_available():
            return jsonify({"error": "Audio conversion is unavailable (ffmpeg missing)."}), 503
    try:
        policy.check_url(url)
    except PolicyError as exc:
        return jsonify({"error": str(exc), "blocked": True}), 403
    if active_jobs() >= MAX_ACTIVE_JOBS:
        return jsonify({"error": "The server is busy with other downloads. Try again in a minute."}), 429

    job_id = str(uuid.uuid4())
    out_dir = os.path.join(DOWNLOAD_DIR, job_id)
    opts = build_ydl_opts(dl_type, quality, fmt, audio_fmt, audio_quality,
                          out_dir, job_id, format_id, has_audio)
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0, "filename": None,
                        "filepath": None, "error": None, "speed": "", "eta": "",
                        "expires": None}
    threading.Thread(target=download_worker, args=(job_id, url, opts), daemon=True).start()
    return jsonify({"job_id": job_id})


def _get_job(job_id):
    if not JOB_ID_RE.match(job_id):
        return None
    purge_expired()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _public(job):
    return {k: job.get(k) for k in ("status", "progress", "filename", "error", "speed", "eta")}


@app.route("/api/status/<job_id>")
def job_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(_public(job))


@app.route("/api/file/<job_id>")
def serve_file(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found or expired."}), 404
    if job["status"] != "done":
        return jsonify({"error": "File not ready yet."}), 409
    path = job["filepath"] or ""
    root = os.path.realpath(DOWNLOAD_DIR) + os.sep
    if not os.path.realpath(path).startswith(root) or not os.path.isfile(path):
        return jsonify({"error": "File expired. Start the download again."}), 410
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return send_file(path, mimetype=mime, as_attachment=True, download_name=job["filename"])


@app.route("/api/progress/<job_id>")
def progress_stream(job_id: str):
    """Server-Sent Events progress stream."""
    def generate():
        deadline = time.time() + SSE_MAX_SECONDS
        while time.time() < deadline:
            job = _get_job(job_id)
            if not job:
                yield 'data: {"status": "error", "error": "Job not found."}\n\n'
                return
            yield f"data: {json.dumps(_public(job))}\n\n"
            if job["status"] in ("done", "error"):
                return
            time.sleep(0.5)

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    print(f"Max Media Downloader (compliant mode) — yt-dlp: {YT_DLP_AVAILABLE}, "
          f"ffmpeg: {ffmpeg_available()}, temp: {DOWNLOAD_DIR}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
