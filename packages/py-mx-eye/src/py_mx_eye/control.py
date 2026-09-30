"""Synchronous REQ/REP control access, with one REQ socket per call."""

import math
import time

import zmq
from mx_eye_protocol.control import (
    Command,
    ControlReply,
    ControlRequest,
    StatusSnapshot,
)


class ControlClient:
    """Independent calls own their sockets, including on different GUI threads."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def rpc(
        self, request: ControlRequest, timeout: float | None = None
    ) -> ControlReply:
        """Send once and wait for a reply. A timeout does not cancel execution."""
        timeout = self.timeout if timeout is None else timeout
        data = request.model_dump_json(exclude_none=True).encode("utf-8")
        deadline = time.monotonic() + timeout
        with zmq.Context() as context, context.socket(zmq.REQ) as sock:
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.IMMEDIATE, 1)
            sock.connect(f"tcp://{self.host}:{self.port}")
            try:
                if not sock.poll(self._remaining_ms(deadline), zmq.POLLOUT):
                    raise TimeoutError("Connection timed out")
                sock.send(data, flags=zmq.NOBLOCK)
                if not sock.poll(self._remaining_ms(deadline), zmq.POLLIN):
                    raise TimeoutError("Reply timed out")
                parts = sock.recv_multipart()
            except (TimeoutError, zmq.ZMQError) as exc:
                raise TimeoutError(
                    f"Tracker did not reply at {self.host}:{self.port}: {exc}"
                ) from exc
        if len(parts) != 1:
            raise ValueError("Expected one control reply frame")
        reply = ControlReply.model_validate_json(parts[0])
        if not reply.ok:
            raise RuntimeError(reply.error or "Tracker rejected command")
        return reply

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        return max(0, math.ceil((deadline - time.monotonic()) * 1000))

    def request_status(
        self, command: Command, timeout: float | None = None
    ) -> StatusSnapshot:
        reply = self.rpc(ControlRequest(command=command), timeout=timeout)
        if reply.status is None:
            raise ValueError("Expected a status response")
        return reply.status
