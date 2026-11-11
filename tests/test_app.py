import os
import time

import pytest

import app


def post(client, path, body):
    return client.post(path, json=body)


def test_health_reports_compliant_mode(client):
    d = client.get("/api/health").get_json()
    assert d["ok"] and d["mode"] == "compliant"
    assert "archive.org" in d["allowed_domains"]


def test_security_headers_and_no_open_cors(client):
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


@pytest.mark.parametrize("path", ["/api/analyze", "/api/download"])
@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=1", "https://x.com/a/status/1"])
def test_blocked_platforms_refused_before_any_fetch(client, monkeypatch, path, url):
    def no_network(*a, **k):
        raise AssertionError("must not touch the network")
    monkeypatch.setattr(app.yt_dlp, "YoutubeDL", no_network)
    r = post(client, path, {"url": url})
    assert r.status_code == 403
    d = r.get_json()
    assert d["blocked"] is True and "Terms of Service" in d["error"]


def test_ssrf_refused(client):
    for u in ["http://127.0.0.1:5000/", "http://169.254.169.254/", "http://localhost/"]:
        assert post(client, "/api/analyze", {"url": u}).status_code == 403


@pytest.mark.parametrize("body,msg", [
    ({"url": "https://archive.org/details/x", "format_id": "best/bestvideo"}, "format id"),
    ({"url": "https://archive.org/details/x", "format_id": "0+bestaudio"}, "format id"),
    ({"url": "https://archive.org/details/x", "type": "../../etc"}, "download type"),
    ({"url": "https://archive.org/details/x", "type": "convert", "audio_format": "mp3;id"}, "audio format"),
    ({"url": "https://archive.org/details/x", "type": "convert", "audio_quality": "1e9"}, "audio format"),
    ({}, "No URL"),
])
def test_download_input_validation(client, body, msg):
    r = post(client, "/api/download", body)
    assert r.status_code == 400 and msg in r.get_json()["error"]


def test_non_json_and_oversized_body(client):
    assert client.post("/api/analyze", data="[1,2]", content_type="application/json").status_code == 400
    r = client.post("/api/analyze", data="x" * 20000, content_type="application/json")
    assert r.status_code == 413


@pytest.mark.parametrize("jid", ["..%2F..%2Fetc%2Fpasswd", "abc", "0" * 36])
def test_bad_job_ids(client, jid):
    assert client.get(f"/api/file/{jid}").status_code == 404
    assert client.get(f"/api/status/{jid}").status_code == 404


def test_concurrency_cap(client, monkeypatch, public_dns):
    monkeypatch.setattr(app, "MAX_ACTIVE_JOBS", 1)
    monkeypatch.setattr(app, "download_worker", lambda *a: None)
    with app.JOBS_LOCK:
        app.JOBS.clear()
    body = {"url": "https://upload.wikimedia.org/wikipedia/commons/c/c8/Example.ogg"}
    assert post(client, "/api/download", body).status_code == 200
    r = post(client, "/api/download", body)
    assert r.status_code == 429
    with app.JOBS_LOCK:
        app.JOBS.clear()


def test_file_outside_download_dir_not_served(client, tmp_path):
    jid = "11111111-1111-1111-1111-111111111111"
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    with app.JOBS_LOCK:
        app.JOBS[jid] = {"status": "done", "progress": 100, "filename": "s.txt",
                         "filepath": str(secret), "error": None, "expires": None}
    assert client.get(f"/api/file/{jid}").status_code == 410
    with app.JOBS_LOCK:
        app.JOBS.pop(jid)


def test_expired_jobs_purged_with_files(client):
    jid = "22222222-2222-2222-2222-222222222222"
    d = os.path.join(app.DOWNLOAD_DIR, jid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "f.mp4"), "w").close()
    with app.JOBS_LOCK:
        app.JOBS[jid] = {"status": "done", "progress": 100, "filename": "f.mp4",
                         "filepath": os.path.join(d, "f.mp4"), "error": None,
                         "expires": time.time() - 1}
    assert client.get(f"/api/status/{jid}").status_code == 404
    assert not os.path.exists(d)


def test_progress_hook_enforces_size_cap(monkeypatch):
    monkeypatch.setattr(app, "MAX_FILESIZE_MB", 1)
    hook = app.make_progress_hook("nojob")
    hook({"status": "downloading", "downloaded_bytes": 1000})
    with pytest.raises(app.TooLarge):
        hook({"status": "downloading", "downloaded_bytes": 2 * 1024 * 1024})


def test_parse_streams_handles_unknown_codecs():
    info = {"formats": [
        {"format_id": "0", "ext": "mp4", "height": 240, "url": "https://archive.org/a.mp4"},
        {"format_id": "1", "ext": "ogv", "height": 304, "url": "https://archive.org/a.ogv"},
        {"format_id": "ogg", "ext": "ogg", "url": "https://upload.wikimedia.org/a.ogg"},
        {"format_id": "bad/id", "ext": "mp4", "height": 720},
    ]}
    s = [x for x in app._parse_streams(info) if x["type"] != "convert"]
    assert [x["format_id"] for x in s] == ["1", "0", "ogg"]
    assert s[0]["type"] == "video" and s[0]["has_audio"] is True
    assert s[2]["type"] == "audio"


def test_format_selection_strings():
    o = app.build_ydl_opts("video", "best", "mp4", "", "", "/tmp/x", "j", "0", False)
    assert o["format"] == "0+bestaudio/0"
    o = app.build_ydl_opts("convert", "best", "", "flac", "0", "/tmp/x", "j")
    assert o["postprocessors"][0]["preferredcodec"] == "flac"
    o = app.build_ydl_opts("video", "720p", "webm", "", "", "/tmp/x", "j")
    assert "[height<=720][ext=webm]" in o["format"] and o["merge_output_format"] == "webm"
    assert o["enable_file_urls"] is False and o["max_filesize"] == app.MAX_FILESIZE_MB * 1024 * 1024


def test_probe_drops_formats_hosted_off_allowlist(monkeypatch, public_dns):
    monkeypatch.setattr(app.policy, "check_license", lambda p: "ok")

    class FakeYDL:
        def __init__(self, o): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False, process=False):
            return {"title": "t", "webpage_url": url, "formats": [
                {"format_id": "a", "ext": "mp4", "url": "https://upload.wikimedia.org/a.mp4"},
                {"format_id": "b", "ext": "mp4", "url": "https://evil.example/b.mp4"},
                {"format_id": "c", "ext": "mp4", "url": "http://169.254.169.254/c.mp4"},
            ]}
    monkeypatch.setattr(app.yt_dlp, "YoutubeDL", FakeYDL)
    info, _ = app.probe("https://commons.wikimedia.org/wiki/File:X.webm")
    assert [f["format_id"] for f in info["formats"]] == ["a"]


def test_probe_refuses_playlists(monkeypatch, public_dns):
    monkeypatch.setattr(app.policy, "check_license", lambda p: "ok")

    class FakeYDL:
        def __init__(self, o): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k): return {"_type": "playlist", "entries": []}
    monkeypatch.setattr(app.yt_dlp, "YoutubeDL", FakeYDL)
    with pytest.raises(app.PolicyError, match="collection"):
        app.probe("https://archive.org/details/x")
