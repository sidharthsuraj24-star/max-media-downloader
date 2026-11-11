import socket

import pytest

import policy
from policy import PolicyError, check_license, check_url


@pytest.mark.parametrize("url,platform", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "YouTube"),
    ("https://youtu.be/dQw4w9WgXcQ", "YouTube"),
    ("https://m.youtube.com/shorts/abc", "YouTube"),
    ("https://x.com/user/status/1", "X (Twitter)"),
    ("https://twitter.com/user/status/1", "X (Twitter)"),
    ("https://mobile.twitter.com/a", "X (Twitter)"),
    ("https://www.instagram.com/p/abc/", "Instagram"),
    ("https://www.tiktok.com/@a/video/1", "TikTok"),
    ("https://fb.watch/abc", "Facebook"),
])
def test_tos_platforms_blocked_with_reason(url, platform):
    with pytest.raises(PolicyError) as e:
        check_url(url, resolve=False)
    assert platform in str(e.value) and "Terms of Service" in str(e.value)


def test_blocked_platform_cannot_be_allowlisted(monkeypatch):
    monkeypatch.setenv("ALLOWED_DOMAINS", "youtube.com,x.com")
    with pytest.raises(PolicyError, match="Terms of Service"):
        check_url("https://youtube.com/watch?v=1", resolve=False)


@pytest.mark.parametrize("url", [
    "https://commons.wikimedia.org/wiki/File:Folgers.ogv",
    "https://upload.wikimedia.org/wikipedia/commons/c/c8/Example.ogg",
    "https://archive.org/details/Popeye_forPresident",
    "https://ia800300.us.archive.org/1/items/x/x.mp4",
])
def test_allowlisted_sources_pass(url, public_dns):
    assert check_url(url).hostname


@pytest.mark.parametrize("url", [
    "https://vimeo.com/1",
    "https://wikimedia.org.evil.com/x",
    "https://evilarchive.org/details/x",
    "https://en.wikipedia.org/wiki/Cat",
])
def test_unlisted_hosts_refused(url, public_dns):
    with pytest.raises(PolicyError, match="not on the allowed list"):
        check_url(url)


def test_operator_domain_can_be_added(monkeypatch, public_dns):
    monkeypatch.setenv("ALLOWED_DOMAINS", "media.example.org")
    assert check_url("https://media.example.org/a.mp4")
    assert check_license(check_url("https://media.example.org/a.mp4")) == "Owner-approved source"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
    "http://10.0.0.1/", "https://archive.org@127.0.0.1/", "https://user:pw@archive.org/",
    "https://archive.org:8080/details/x", "file:///etc/passwd", "ftp://archive.org/x",
    "gopher://archive.org/", "javascript:alert(1)", "", "https://", "x" * 3000,
])
def test_ssrf_and_malformed_refused(url):
    with pytest.raises(PolicyError):
        check_url(url, resolve=False)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "::1", "fd00::1"])
def test_allowlisted_host_resolving_private_is_refused(monkeypatch, ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(fam, socket.SOCK_STREAM, 6, "", (ip, 443))])
    with pytest.raises(PolicyError, match="private network"):
        check_url("https://archive.org/details/x")


def test_archive_license_open_passes(public_dns):
    p = check_url("https://archive.org/details/Popeye_forPresident")
    lic = check_license(p, fetch=lambda i: "http://creativecommons.org/licenses/publicdomain/")
    assert "publicdomain" in lic


@pytest.mark.parametrize("lic", ["", "http://example.com/all-rights-reserved", "https://creativecommons.org.evil/licenses/by/4.0/"])
def test_archive_license_missing_or_closed_refused(public_dns, lic):
    p = check_url("https://archive.org/details/Some_Film")
    with pytest.raises(PolicyError, match="no open licence"):
        check_license(p, fetch=lambda i: lic)


def test_archive_license_fetch_failure_is_refused(public_dns):
    def boom(i):
        raise OSError("timeout")
    with pytest.raises(PolicyError, match="Could not check"):
        check_license(check_url("https://archive.org/details/x"), fetch=boom)


def test_archive_identifier_parsing(public_dns):
    assert policy.archive_identifier(check_url("https://archive.org/download/abc_1/f.mp4")) == "abc_1"
    assert policy.archive_identifier(check_url("https://archive.org/search?q=x")) is None
