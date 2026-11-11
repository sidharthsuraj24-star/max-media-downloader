"""Real-network end-to-end checks. Run with: LIVE=1 python -m pytest tests/test_live.py"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 for network tests")


def _run(client, body, timeout=180):
    r = client.post("/api/download", json=body)
    assert r.status_code == 200, r.get_json()
    jid = r.get_json()["job_id"]
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/api/status/{jid}").get_json()
        if s["status"] in ("done", "error"):
            break
        time.sleep(1)
    assert s["status"] == "done", s
    f = client.get(f"/api/file/{jid}")
    assert f.status_code == 200
    return s, f.data


def test_wikimedia_video(client):
    a = client.post("/api/analyze", json={"url": "https://commons.wikimedia.org/wiki/File:Folgers.ogv"})
    assert a.status_code == 200
    vids = [s for s in a.get_json()["streams"] if s["type"] == "video"]
    assert vids
    small = min(vids, key=lambda s: float((s["size"].strip("~ MB") or "0").replace("?", "0") or 0))
    _s, data = _run(client, {"url": "https://commons.wikimedia.org/wiki/File:Folgers.ogv",
                            "type": "video", "format_id": small["format_id"], "has_audio": small["has_audio"]})
    assert len(data) > 100_000


def test_wikimedia_audio_convert_mp3(client):
    s, data = _run(client, {"url": "https://upload.wikimedia.org/wikipedia/commons/c/c8/Example.ogg",
                            "type": "convert", "audio_format": "mp3", "audio_quality": "192"})
    assert s["filename"].endswith(".mp3") and data[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")


def test_archive_public_domain_item(client):
    a = client.post("/api/analyze", json={"url": "https://archive.org/details/Popeye_forPresident"})
    assert a.status_code == 200 and "publicdomain" in a.get_json()["license"]


def test_youtube_refused(client):
    r = client.post("/api/analyze", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert r.status_code == 403
