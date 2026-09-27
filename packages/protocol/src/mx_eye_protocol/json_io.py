"""One newline-delimited Pydantic JSON message per request/response connection."""

import socket
from typing import TypeVar

from pydantic import BaseModel

Message = TypeVar("Message", bound=BaseModel)


def receive_json(sock: socket.socket, model: type[Message]) -> Message:
    """Read one bounded JSON line and validate it directly into a model."""
    data = bytearray()
    while b"\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Peer closed before replying")
        data.extend(chunk)
        if len(data) > 16384:
            raise ValueError("Control message is too large")
    return model.model_validate_json(bytes(data.split(b"\n", 1)[0]))


def send_json(sock: socket.socket, message: BaseModel) -> None:
    sock.sendall(message.model_dump_json(exclude_none=True).encode("utf-8") + b"\n")
