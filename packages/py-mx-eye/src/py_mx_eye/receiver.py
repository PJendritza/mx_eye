"""Synchronous ZeroMQ subscriber and tracking-message decoder.

The caller owns the SUB socket's thread. libzmq handles transport I/O internally;
the SDK creates no Python background receiver thread.
"""

import math
import time
from collections import deque
from dataclasses import dataclass

import zmq
from mx_eye_protocol.data_frame import TRACKING_FIELDS, DataFrame

from .sample import Sample


def decode_frame(data: bytes) -> DataFrame:
    """Decode exactly one headerless tracking message."""
    if len(data) != DataFrame.TRACKING.size:
        raise ValueError("Invalid mx_eye frame size")
    return DataFrame.model_validate(
        dict(zip(TRACKING_FIELDS, DataFrame.TRACKING.unpack(data), strict=True))
    )


@dataclass(frozen=True)
class ReceiverConfig:
    """The tracker publishing endpoint."""

    host: str
    port: int


class SampleReceiver:
    """One subscriber, opened, read and closed on the caller's thread."""

    def __init__(self, config: ReceiverConfig) -> None:
        self.host = config.host
        self.port = config.port
        self._context: zmq.Context[zmq.Socket[bytes]] | None = None
        self._sock: zmq.Socket[bytes] | None = None
        self._session: int | None = None
        self._sequence: int | None = None
        self._old_sessions: deque[int] = deque(maxlen=32)

    def open(self) -> None:
        """Subscribe asynchronously; the publisher need not be online yet."""
        if self._sock is not None:
            return
        context = zmq.Context()
        sock = context.socket(zmq.SUB)
        try:
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.RCVHWM, 64)
            sock.setsockopt(zmq.SUBSCRIBE, b"")
            sock.connect(f"tcp://{self.host}:{self.port}")
        except Exception:
            sock.close(linger=0)
            context.term()
            raise
        self._context, self._sock = context, sock

    def close(self) -> None:
        """Discard queued samples and release this subscriber's resources."""
        sock, self._sock = self._sock, None
        context, self._context = self._context, None
        if sock is not None:
            sock.close(linger=0)
        if context is not None:
            context.term()

    def next_sample(self, timeout: float | None = None) -> Sample | None:
        """Return an accepted sample or None on timeout, skipping bad messages."""
        sock = self._sock
        if sock is None:
            raise RuntimeError("Sample stream is closed; connect again")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait_ms = (
                None
                if deadline is None
                else max(0, math.ceil((deadline - time.monotonic()) * 1000))
            )
            if not sock.poll(wait_ms, zmq.POLLIN):
                return None
            parts = sock.recv_multipart()
            receive_ns = time.time_ns()
            if len(parts) == 1:
                try:
                    frame = decode_frame(parts[0])
                except ValueError:
                    pass  # One malformed message does not lose stream boundaries.
                else:
                    sample = self._accept(Sample(frame=frame, receive_ns=receive_ns))
                    if sample is not None:
                        return sample
            if timeout != 0 and deadline is not None and time.monotonic() >= deadline:
                return None

    def _accept(self, sample: Sample) -> Sample | None:
        """Apply session and sequence policy to one received sample."""
        payload = sample.frame
        session, sequence = payload.session, payload.sequence
        if session != self._session:
            if session in self._old_sessions:
                return None
            if self._session is not None:
                self._old_sessions.append(self._session)
            self._session = session
            self._sequence = None
        if self._sequence is not None and sequence <= self._sequence:
            return None
        self._sequence = sequence
        return sample
