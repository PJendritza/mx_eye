"""Single-frame synchronous REQ/REP exchange, without automatic retries."""

import math
import time

import zmq


def request_reply(host: str, port: int, timeout: float, data: bytes) -> bytes:
    """A fresh socket per call prevents a timeout from poisoning the next RPC."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("RPC timeout must be finite and non-negative")
    deadline = time.monotonic() + timeout

    def remaining_ms() -> int:
        return max(0, math.ceil((deadline - time.monotonic()) * 1000))

    with zmq.Context() as context, context.socket(zmq.REQ) as sock:
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.IMMEDIATE, 1)
        sock.connect(f"tcp://{host}:{port}")
        try:
            if not sock.poll(remaining_ms(), zmq.POLLOUT):
                raise TimeoutError("Connection timed out")
            sock.send(data, flags=zmq.NOBLOCK)
            if not sock.poll(remaining_ms(), zmq.POLLIN):
                raise TimeoutError("Reply timed out")
            parts = sock.recv_multipart()
        except (TimeoutError, zmq.ZMQError) as exc:
            raise TimeoutError(
                f"Tracker did not reply at {host}:{port}: {exc}"
            ) from exc
    if len(parts) != 1:
        raise ValueError("Expected one RPC reply frame")
    return parts[0]
