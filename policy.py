"""Source policy: which URLs the server is willing to fetch.

The service runs in compliant mode: it only downloads from sources whose
content is openly licensed (Wikimedia Commons, Internet Archive items that
carry an open licence) plus any domains the operator owns and lists in
ALLOWED_DOMAINS. Platforms whose terms forbid downloading (YouTube, X and
similar) are refused outright and cannot be re-enabled through config.

Every URL the server touches -- the page the user pasted and each media URL
yt-dlp resolves from it -- goes through check_url(), which also refuses
hosts that resolve to private, loopback or link-local addresses (SSRF).
"""

import ipaddress
import json
import os
import re
import socket
import urllib.request
from urllib.parse import unquote, urlparse

DEFAULT_ALLOWED_DOMAINS = (
    "commons.wikimedia.org",
    "upload.wikimedia.org",
    "archive.org",
)

# name shown to the user -> domains (subdomains included)
BLOCKED_PLATFORMS = {
    "YouTube": ("youtube.com", "youtu.be", "youtube-nocookie.com", "ytimg.com",
                "googlevideo.com"),
    "X (Twitter)": ("x.com", "twitter.com", "t.co", "twimg.com"),
    "Instagram": ("instagram.com", "cdninstagram.com"),
    "Facebook": ("facebook.com", "fb.watch", "fbcdn.net"),
    "TikTok": ("tiktok.com", "tiktokcdn.com"),
}

OPEN_LICENSE_RE = re.compile(
    r"^https?://(www\.)?creativecommons\.org/(licenses|publicdomain)/", re.IGNORECASE)

ARCHIVE_ID_RE = re.compile(r"^/(?:details|download|embed)/([A-Za-z0-9._-]+)")


class PolicyError(Exception):
    """URL refused by policy. str(exc) is safe to show to the user."""


def _env_domains(name):
    raw = os.environ.get(name, "")
    return tuple(d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip())


def allowed_domains():
    return DEFAULT_ALLOWED_DOMAINS + _env_domains("ALLOWED_DOMAINS")


def require_open_license():
    return os.environ.get("REQUIRE_OPEN_LICENSE", "1").strip() not in ("0", "false", "no")


def _matches(host, domain):
    return host == domain or host.endswith("." + domain)


def blocked_platform(host):
    host = (host or "").lower().rstrip(".")
    for name, domains in BLOCKED_PLATFORMS.items():
        if any(_matches(host, d) for d in domains):
            return name
    return None


def allowed_sources_text():
    return "Wikimedia Commons and Internet Archive (openly licensed items)"


def _extra_allowed_nets():
    # Only for sandboxes whose DNS returns synthetic addresses (e.g. fake-IP
    # proxies using 198.18.0.0/15). Never needed on a normal host.
    nets = []
    for raw in os.environ.get("SSRF_ALLOW_NETS", "").split(","):
        if raw.strip():
            nets.append(ipaddress.ip_network(raw.strip(), strict=False))
    return nets


def _resolve_is_public(host, port):
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise PolicyError(f"Could not resolve {host}.")
    if not infos:
        raise PolicyError(f"Could not resolve {host}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global and not any(ip in n for n in _extra_allowed_nets()):
            raise PolicyError("That address points to a private network and is not allowed.")


def check_url(url, resolve=True):
    """Return the parsed URL if allowed, else raise PolicyError."""
    if not isinstance(url, str) or len(url) > 2048:
        raise PolicyError("Invalid URL.")
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        raise PolicyError("Invalid URL.")
    if p.scheme not in ("http", "https") or not p.hostname:
        raise PolicyError("Invalid URL. Only http(s) links are supported.")
    if p.username or p.password:
        raise PolicyError("Links with embedded credentials are not allowed.")
    host = p.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        raise PolicyError("Links to raw IP addresses are not allowed.")
    except ValueError:
        pass
    if port not in (None, 80, 443):
        raise PolicyError("Links on non-standard ports are not allowed.")

    platform = blocked_platform(host)
    if platform:
        raise PolicyError(
            f"Downloads from {platform} are not supported: its Terms of Service "
            f"prohibit downloading. Try openly licensed media from "
            f"{allowed_sources_text()}.")
    if not any(_matches(host, d) for d in allowed_domains()):
        raise PolicyError(
            f"{host} is not on the allowed list. This service only downloads "
            f"openly licensed or owner-approved media from {allowed_sources_text()}.")
    if resolve:
        _resolve_is_public(host, port or (443 if p.scheme == "https" else 80))
    return p


def archive_identifier(parsed):
    host = parsed.hostname.lower()
    if not _matches(host, "archive.org"):
        return None
    m = ARCHIVE_ID_RE.match(unquote(parsed.path))
    return m.group(1) if m else None


def fetch_archive_license(identifier, timeout=15):
    """Return the item's licenseurl ('' if none). Network call."""
    url = f"https://archive.org/metadata/{identifier}/metadata"
    req = urllib.request.Request(url, headers={"User-Agent": "max-media-downloader"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # host is fixed
        data = json.loads(r.read(1_000_000) or b"{}")
    result = data.get("result") or {}
    lic = result.get("licenseurl") or ""
    return lic if isinstance(lic, str) else ""


def check_license(parsed, fetch=fetch_archive_license):
    """Return a licence label for display; raise PolicyError if not open.

    Wikimedia Commons only hosts freely licensed media by policy. Internet
    Archive hosts everything, so each item's own licence is checked.
    Operator-owned ALLOWED_DOMAINS are trusted.
    """
    host = parsed.hostname.lower()
    if _matches(host, "wikimedia.org"):
        return "Free licence (Wikimedia Commons)"
    if _matches(host, "archive.org"):
        ident = archive_identifier(parsed)
        if not ident:
            if require_open_license():
                raise PolicyError("Use an Internet Archive item link (archive.org/details/...).")
            return ""
        lic = None
        for _ in range(2):
            try:
                lic = fetch(ident)
                break
            except Exception:  # noqa: BLE001 - network hiccup, retry once
                continue
        if lic is None:
            raise PolicyError("Could not check this Internet Archive item's licence. Try again.")
        if OPEN_LICENSE_RE.match(lic):
            return lic
        if require_open_license():
            raise PolicyError(
                "This Internet Archive item has no open licence (Creative Commons "
                "or public domain) on record, so it can't be downloaded here.")
        return lic
    return "Owner-approved source"
