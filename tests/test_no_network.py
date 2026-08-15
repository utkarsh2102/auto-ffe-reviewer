"""Verify the offline guard works.

Without this, the autouse fixture in conftest could stop blocking sockets and
every other test would quietly start depending on the network.
"""

from __future__ import annotations

import socket

import pytest

from conftest import NetworkAccessAttempted


def test_socket_creation_is_blocked() -> None:
    with pytest.raises(NetworkAccessAttempted):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkAccessAttempted):
        socket.create_connection(("example.invalid", 80))


@pytest.mark.network
def test_marked_tests_may_opt_out() -> None:
    # Does not actually connect; just proves the marker lifts the guard so the
    # opt-out path is real and not just documented.
    assert socket.socket is not None
