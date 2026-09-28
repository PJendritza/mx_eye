"""Receiver-side data plane: decode one frame, frame the byte stream, read.

Socket bytes are cut into frames by FrameAssembler, decoded into DataFrame
models by decode_frame, and filtered by session and sequence. Only
SampleReceiver touches a socket, and it owns no thread: the caller's thread
drives every read, so the SDK adds no concurrency of its own. Whether an
accepted sample is fresh enough to use is decided by the caller of this layer.
"""

import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from mx_eye_protocol.control import Transport
from mx_eye_protocol.data_frame import (
    TRACKING_FIELDS,
    DataFrame,
    MessageType,
    TrackingPayload,
)

from .control import Connector
from .sample import Sample


# --- one frame: bytes <-> model --------------------------------------------
def decode_header(data: bytes) -> tuple[bytes, MessageType, int]:
    """Validate an incoming DATA header before buffering its payload."""
    if len(data) != DataFrame.header_size:
        raise ValueError("Invalid mx_eye frame header size")
    magic, kind, length = DataFrame.HEADER.unpack(data)
    if magic != DataFrame.magic:
        raise ValueError("Invalid mx_eye frame magic")
    try:
        message_type = MessageType(kind)
    except ValueError as exc:
        raise ValueError("Unknown mx_eye message type") from exc
    if message_type is MessageType.CMD:
        raise ValueError("CMD payloads are not implemented")
    if length != DataFrame.TRACKING.size:
        raise ValueError("Invalid mx_eye tracking payload length")
    return magic, message_type, length


def decode_frame(data: bytes) -> DataFrame:
    """Decode exactly one complete DATA frame."""
    header_size = DataFrame.header_size

    magic, message_type, length = decode_header(data[:header_size])

    if len(data) != header_size + length:
        raise ValueError("Invalid mx_eye frame size")

    payload = TrackingPayload.model_validate(
        dict(
            zip(
                TRACKING_FIELDS,
                DataFrame.TRACKING.unpack(data[header_size:]),
                strict=True,
            )
        )
    )

    return DataFrame(
        magic=magic,
        message_type=message_type,
        length=length,
        payload=payload,
    )


# --- the byte stream -> frames --------------------------------------------
class FrameError(ValueError):
    """A malformed frame header: the stream boundary is lost.

    The assembler clears its state before raising, so the caller must discard
    the connection and start a new one.
    """


class FrameAssembler:
    """Accumulate bytes one at a time and emit a frame when one completes.

    The state is either ``_expected is None`` (the header is still incomplete)
    or ``_expected`` total frame bytes (the header is known and its payload is
    arriving). Each header is therefore parsed exactly once.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected: int | None = None

    def reset(self) -> None:
        """Discard any partial frame, for example after a disconnect."""
        self._buffer.clear()
        self._expected = None

    def feed(self, byte: int) -> bytes | None:
        """Advance one byte, returning the frame that byte completes, if any.

        ``byte`` is an unsigned octet, as produced by iterating over ``bytes``.
        Iterate a received chunk and handle each returned frame before feeding
        the next byte, so a frame is delivered even if a later byte is a
        malformed header.
        """
        self._buffer.append(byte)
        if self._expected is None:
            if len(self._buffer) < DataFrame.header_size:
                return None
            try:
                _, _, length = decode_header(bytes(self._buffer))
            except ValueError as exc:
                self.reset()
                raise FrameError(str(exc)) from exc
            self._expected = DataFrame.header_size + length
        if len(self._buffer) < self._expected:
            return None
        frame = bytes(self._buffer)
        self.reset()
        return frame


# --- frames -> samples ----------------------------------------------------
@dataclass(frozen=True)
class ReceiverConfig:
    """Where to receive one tracker stream.

    ``connect`` and ``datagram`` are socket seams: production uses the standard
    socket calls, tests substitute their own.
    """

    host: str
    port: int
    udp_bind: str = "0.0.0.0"
    connect: Connector = socket.create_connection
    datagram: Callable[[int, int], socket.socket] = socket.socket


class SampleReceiver:
    """Synchronous data stream for one tracker, driven by the caller's thread.

    ``open`` connects (TCP) or binds (UDP), ``next_sample`` returns one accepted
    sample or None when its timeout expires, and ``close`` releases the socket.
    The only state between calls is the frame still arriving plus the samples
    already decoded from the last received chunk.
    """

    def __init__(self, config: ReceiverConfig) -> None:
        self.host = config.host
        self.port = config.port
        self.udp_bind = config.udp_bind
        self._connect = config.connect
        self._datagram = config.datagram
        self._transport: Transport | None = None
        self._sock: socket.socket | None = None
        self._assembler = FrameAssembler()
        self._received: deque[Sample] = deque()
        self._failure: Exception | None = None
        self._session: int | None = None
        self._sequence: int | None = None
        self._old_sessions: deque[int] = deque(maxlen=32)

    def open(self, transport: Transport, timeout: float) -> None:
        """Connect (TCP) or bind (UDP) the data socket.

        Socket failures propagate, so an absent tracker is reported here instead
        of turning into an empty stream. Opening an open stream does nothing.
        """
        if self._sock is not None:
            return
        if transport is Transport.UDP:
            sock = self._datagram(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind((self.udp_bind, self.port))
        else:
            sock = self._connect((self.host, self.port), timeout)
        self._transport = transport
        self._sock = sock
        self._assembler.reset()
        self._received.clear()
        self._failure = None

    def close(self) -> None:
        """Release the socket and forget any partially received frame."""
        sock, self._sock = self._sock, None
        self._assembler.reset()
        self._received.clear()
        self._failure = None
        if sock is not None:
            sock.close()

    def next_sample(self, timeout: float | None = None) -> Sample | None:
        """The next accepted sample, or None when ``timeout`` expires.

        Samples that repeat a sequence, arrive out of order, or belong to a
        session already left behind are skipped without ending the read. A
        malformed TCP header loses the stream boundary: the samples decoded
        before it are still returned, and the following call raises FrameError.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._received:
                sample = self._accept(self._received.popleft())
                if sample is not None:
                    return sample
                continue
            if self._failure is not None:
                failure, self._failure = self._failure, None
                raise failure
            if not self._fill(deadline):
                return None

    def _fill(self, deadline: float | None) -> bool:
        """Receive one chunk or datagram; False when the timeout expires.

        A closed peer ends the stream like a malformed header does: the samples
        already decoded are still returned, then the failure is raised.
        """
        sock = self._sock
        if sock is None:
            raise RuntimeError("Sample stream is closed; connect again")
        sock.settimeout(self._remaining(deadline))
        try:
            if self._transport is Transport.UDP:
                # One datagram carries exactly one frame, never a partial one.
                data, _ = sock.recvfrom(2048)
                self._enqueue(data)
            else:
                chunk = sock.recv(65536)
                if not chunk:
                    self._fail(ConnectionError("Tracker closed the sample stream"))
                    return True
                self._feed(chunk)
        except (TimeoutError, BlockingIOError):
            return False
        except OSError as exc:
            failure = ConnectionError(f"Sample stream failed: {exc}")
            self._fail(failure)
            raise failure from exc
        return True

    def _feed(self, chunk: bytes) -> None:
        """Decode every frame in one chunk; a bad header ends the stream."""
        for byte in chunk:
            try:
                frame = self._assembler.feed(byte)
            except FrameError as exc:
                # Keep the samples decoded so far and report on the next call.
                self._fail(exc)
                return
            if frame is not None:
                self._enqueue(frame)

    def _fail(self, failure: Exception) -> None:
        """End the stream, keeping the samples already decoded for this call."""
        self._failure = failure
        sock, self._sock = self._sock, None
        self._assembler.reset()
        if sock is not None:
            sock.close()

    def _enqueue(self, data: bytes) -> None:
        """Decode one frame into an undecided sample, dropping what will not decode."""
        try:
            frame = decode_frame(data)
        except ValueError:
            # A datagram or a frame that decodes badly is one lost sample, not
            # a lost stream boundary; the connection stays usable.
            return
        self._received.append(Sample(frame=frame, receive_ns=time.time_ns()))

    def _accept(self, sample: Sample) -> Sample | None:
        """Apply session and sequence policy to one received sample."""
        payload = sample.frame.payload
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

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        """Seconds left before ``deadline``; None means wait forever."""
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())
