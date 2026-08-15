"""Shared test fixtures.

The suite is offline by design: no test may open a socket, and none needs an API
key. Anything that would reach the network must be exercised through frozen
fixtures under tests/fixtures/raw/ instead, so the real parsers run against real
bytes without depending on an upstream service being up.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to reach the network."""


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all socket use for the whole suite.

    Opt out with @pytest.mark.network for a test that genuinely needs the
    internet; CI excludes those. See test_no_network.py, which asserts this
    guard actually fires -- without that, the guard could silently rot.
    """
    if request.node.get_closest_marker("network"):
        return

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkAccessAttempted(
            "This test tried to open a socket. Use a fixture under "
            "tests/fixtures/raw/ instead, or mark the test @pytest.mark.network."
        )

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
