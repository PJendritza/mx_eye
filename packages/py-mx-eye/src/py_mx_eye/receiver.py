"""Receiver-side data plane: decode one frame, frame the byte stream, receive.

Socket bytes are cut into frames by FrameAssembler, decoded into DataFrame
models by decode_frame, filtered by session and sequence, and stored as
Samples. Only SampleReceiver touches a socket; framing and decoding stay pure
and testable on their own.
"""

import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from mx_eye_protocol.control import Transport
from mx_eye_protocol.data_frame import (
    DataFrame,
    MessageType,
    TrackingFlags,
    TrackingPayload,
)

from .control import ClockState, Connector
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
    """Decode exactly one complete DATA frame, retaining its header fields."""
    magic, message_type, length = decode_header(data[: DataFrame.header_size])
    if len(data) != DataFrame.header_size + length:
        raise ValueError("Invalid mx_eye frame size")
    (
        session,
        sequence,
        frame,
        acquisition_ns,
        tracking_start_ns,
        tracking_end_ns,
        send_ns,
        media_ns,
        x,
        y,
        pupil_x,
        pupil_y,
        cr_x,
        cr_y,
        pupil_area,
        template_ncc,
        flags,
    ) = DataFrame.TRACKING.unpack(data[DataFrame.header_size :])
    payload = TrackingPayload(
        session=session,
        sequence=sequence,
        frame=frame,
        acquisition_ns=acquisition_ns,
        tracking_start_ns=tracking_start_ns,
        tracking_end_ns=tracking_end_ns,
        send_ns=send_ns,
        media_ns=media_ns,
        x=x,
        y=y,
        pupil_x=pupil_x,
        pupil_y=pupil_y,
        cr_x=cr_x,
        cr_y=cr_y,
        pupil_area=pupil_area,
        template_ncc=template_ncc,
        flags=TrackingFlags(flags),
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

    @property
    def pending(self) -> int:
        """Bytes buffered for the frame currently being assembled."""
        return len(self._buffer)

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
@dataclass
class ReceiverStats:
    """Counters the receive loop mutates in place for every frame.

    Mutable and unvalidated by design: this is data-plane accounting, so a
    dataclass is cheaper than a validated model on the hot path.
    """

    received: int = 0
    sequence_gaps: int = 0
    acquisition_skips: int = 0
    malformed: int = 0
    out_of_order: int = 0
    buffer_overwrites: int = 0
    error: str = ""


class SampleReceiver:
    """Bounded, non-blocking receiver for one tracker session stream."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        udp_bind: str,
        max_samples: int,
        clock_state: Callable[[], ClockState],
        now: Callable[[], int] = time.perf_counter_ns,
        connect: Connector = socket.create_connection,
        datagram: Callable[[int, int], socket.socket] = socket.socket,
    ) -> None:
        self.host = host
        self.port = port
        self.udp_bind = udp_bind
        self._clock_state = clock_state
        self._now = now
        self._connect = connect
        self._datagram = datagram
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._samples: deque[Sample] = deque(maxlen=max_samples)
        self._latest: Sample | None = None
        self._thread: threading.Thread | None = None
        self._session: int | None = None
        self._sequence: int | None = None
        self._frame: int | None = None
        self._old_sessions: deque[int] = deque(maxlen=32)
        self._counters = ReceiverStats()

    def start(self, transport: Transport) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self.run, args=(transport,), daemon=True, name="mx-eye receive"
        )
        self._thread.start()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=4)

    @property
    def error(self) -> str:
        with self._lock:
            return self._counters.error

    def reset_error(self) -> None:
        with self._lock:
            self._counters.error = ""

    def latest(self) -> Sample | None:
        with self._lock:
            return self._latest

    def drain(self) -> list[Sample]:
        """Return and clear buffered samples. Receiving never waits for this."""
        with self._lock:
            samples = list(self._samples)
            self._samples.clear()
        return samples

    def counters(self) -> ReceiverStats:
        with self._lock:
            return replace(self._counters)

    def run(self, transport: Transport) -> None:
        """Socket lifecycle only; framing and per-frame policy are delegated."""
        sock: socket.socket | None = None
        assembler = FrameAssembler()
        try:
            if transport is Transport.UDP:
                sock = self._datagram(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                sock.bind((self.udp_bind, self.port))
                sock.settimeout(0.1)
            self._ready.set()
            while not self._stop.is_set():
                if transport is Transport.TCP:
                    if sock is None:
                        try:
                            sock = self._connect((self.host, self.port), 0.3)
                            sock.settimeout(0.1)
                            assembler.reset()
                        except OSError:
                            sock = None
                            self._stop.wait(0.1)
                            continue
                    try:
                        chunk = sock.recv(65536)
                    except TimeoutError:
                        continue
                    except OSError:
                        sock.close()
                        sock = None
                        assembler.reset()
                        continue
                    if not chunk:
                        # The peer closed; a partial frame is unusable.
                        sock.close()
                        sock = None
                        assembler.reset()
                        continue
                    if not self._consume(chunk, assembler):
                        # An invalid header loses the stream boundary.
                        sock.close()
                        sock = None
                        assembler.reset()
                        continue
                else:
                    if sock is None:
                        # Unreachable for the Transport enum; keep the loop typed.
                        raise RuntimeError("Datagram socket is not open")
                    try:
                        data, _ = sock.recvfrom(2048)
                    except TimeoutError:
                        continue
                    # One datagram carries exactly one frame, never a partial one.
                    self._accept(data)
        except Exception as exc:  # noqa: BLE001 - report failures at the receiver boundary
            with self._lock:
                self._counters.error = str(exc)
            self._ready.set()
        finally:
            if sock is not None:
                sock.close()

    def _consume(self, chunk: bytes, assembler: FrameAssembler) -> bool:
        """Drive the framing state machine over one received chunk.

        Returns False when a malformed header ended the stream; the frames that
        completed before it are delivered first.
        """
        try:
            for byte in chunk:
                frame = assembler.feed(byte)
                if frame is not None:
                    self._accept(frame)
        except FrameError:
            with self._lock:
                self._counters.malformed += 1
            return False
        return True

    def _accept(self, data: bytes) -> None:
        """Decode one complete frame, then apply session, order and timing policy."""
        received = self._now()
        try:
            data_frame = decode_frame(data)
        except ValueError:
            with self._lock:
                self._counters.malformed += 1
            return
        with self._lock:
            session = data_frame.payload.session
            seq = data_frame.payload.sequence
            frame = data_frame.payload.frame
            if session != self._session:
                if session in self._old_sessions:
                    return
                if self._session is not None:
                    self._old_sessions.append(self._session)
                self._session = session
                self._sequence = self._frame = None
                self._samples.clear()
            if self._sequence is not None and self._frame is not None:
                if seq <= self._sequence:
                    self._counters.out_of_order += 1
                    return
                self._counters.sequence_gaps += seq - self._sequence - 1
                # This includes unavailable packets as well as skipped capture frames.
                self._counters.acquisition_skips += max(
                    0, frame - self._frame - (seq - self._sequence)
                )
            self._sequence, self._frame = seq, frame
            clock = self._clock_state()
            sample = Sample(
                frame=data_frame,
                receive_ns=received,
                clock_offset_ns=clock.offset_ns,
                clock_valid_until_ns=clock.valid_until_ns,
                sync_rtt_ms=clock.rtt_ms,
            )
            if len(self._samples) == self._samples.maxlen:
                self._counters.buffer_overwrites += 1
            self._samples.append(sample)
            self._latest = sample
            self._counters.received += 1
