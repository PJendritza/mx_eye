"""Concurrent TCP request/response server for control and clock synchronization."""

import socket
import socketserver
import time
from collections.abc import Callable
from typing import cast

from mx_eye_protocol.control import Reply, Request
from mx_eye_protocol.json_io import receive_json, send_json


class _RequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(3)
        server = cast(ControlServer, self.server)
        try:
            request = receive_json(self.request, Request)
            received_ns = time.perf_counter_ns()
            reply = server.dispatch(request, received_ns)
        except Exception as exc:  # noqa: BLE001 - report failures at the command boundary
            reply = Reply(ok=False, error=str(exc) or type(exc).__name__)
        try:
            send_json(self.request, reply)
        except OSError:
            pass  # The requesting client may have timed out or disconnected.


class ControlServer(socketserver.ThreadingTCPServer):
    """Each connection handles one request and one reply, then closes."""

    daemon_threads = True
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def __init__(
        self,
        address: tuple[str, int],
        dispatch: Callable[[Request, int], Reply],
    ) -> None:
        self.dispatch = dispatch
        super().__init__(address, _RequestHandler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
