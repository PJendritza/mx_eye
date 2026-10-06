"""Synchronous REQ/REP control access, with one REQ socket per call."""

from mx_eye_protocol.control import (
    Command,
    ControlReply,
    ControlRequest,
    StatusSnapshot,
)

from ._rpc import request_reply


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
        reply = ControlReply.model_validate_json(
            request_reply(self.host, self.port, timeout, data)
        )
        if not reply.ok:
            raise RuntimeError(reply.error or "Tracker rejected command")
        return reply

    def request_status(
        self, command: Command, timeout: float | None = None
    ) -> StatusSnapshot:
        reply = self.rpc(ControlRequest(command=command), timeout=timeout)
        if reply.status is None:
            raise ValueError("Expected a status response")
        return reply.status
