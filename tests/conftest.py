import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every host to a public address so tests need no network."""
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def client():
    import app
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c
