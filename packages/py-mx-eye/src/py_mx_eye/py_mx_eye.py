"""Small thread-based receiver SDK; no Qt/OpenCV dependency.

Client receives continuously even when the caller is busy drawing. Cross-host
age is estimated with a four-timestamp exchange, not by comparing raw clocks.
"""

import math
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

from mx_eye_protocol.control import (
    Command,
    Reply,
    Request,
    StatusSnapshot,
    Transport,
)
from mx_eye_protocol.data_frame import DataFrame
from mx_eye_protocol.json_io import receive_json, send_json

from ._decoder import decode_frame, decode_header


@dataclass(frozen=True)
class Sample:
    """A received DATA frame with receiver-local timing information."""

    frame: DataFrame
    receive_ns: int
    clock_offset_ns: float = math.nan  # server minus receiver
    clock_valid_until_ns: int = 0
    sync_rtt_ms: float = math.nan

    @property
    def clock_valid(self) -> bool:
        return (
            math.isfinite(self.clock_offset_ns)
            and time.perf_counter_ns() <= self.clock_valid_until_ns
        )

    @property
    def network_ms(self) -> float:
        return (
            (self.receive_ns - self.frame.payload.send_ns + self.clock_offset_ns) / 1e6
            if self.clock_valid
            else math.nan
        )

    @property
    def arrival_age_ms(self) -> float:
        return (
            (self.receive_ns - self.frame.payload.acquisition_ns + self.clock_offset_ns)
            / 1e6
            if self.clock_valid
            else math.nan
        )

    @property
    def age_ms(self) -> float:
        """Age now, including time the sample has waited in the user's code."""
        return (
            (
                time.perf_counter_ns()
                - self.frame.payload.acquisition_ns
                + self.clock_offset_ns
            )
            / 1e6
            if self.clock_valid
            else math.nan
        )


class Client:
    def __init__(
        self,
        host="127.0.0.1",
        data_port=5556,
        control_port=5557,
        transport: Transport | str | None = None,
        udp_bind="0.0.0.0",
        buffer_samples=4096,
        timeout=3.0,
    ):
        self.host, self.data_port = host, data_port
        self.control_port = control_port
        self.transport = Transport(transport) if transport is not None else None
        self.udp_bind = udp_bind
        self.timeout = timeout
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._samples = deque(maxlen=buffer_samples)
        self._latest = None
        self._threads = []
        self._clock = (math.nan, 0, math.nan)
        self._session = None
        self._sequence = self._frame = None
        self._old_sessions = deque(maxlen=32)
        self._stats = dict(
            received=0,
            sequence_gaps=0,
            acquisition_skips=0,
            malformed=0,
            out_of_order=0,
            buffer_overwrites=0,
            error="",
            sync_rtt_ms=math.nan,
            clock_synced=False,
        )

    def _rpc(self, request: Request, timeout=None) -> Reply:
        timeout = self.timeout if timeout is None else timeout
        try:
            with socket.create_connection(
                (self.host, self.control_port), timeout
            ) as sock:
                sock.settimeout(timeout)
                send_json(sock, request)
                reply = receive_json(sock, Reply)
                if not reply.ok:
                    raise RuntimeError(reply.error or "Tracker rejected command")
                return reply
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TimeoutError(
                f"Tracker did not reply at {self.host}:{self.control_port}: {exc}"
            ) from exc

    def connect(self):
        if self._threads:
            return self
        status = self.status()
        self._stats["error"] = ""
        self.transport = self.transport or status.network.transport
        self._stop.clear()
        self._ready = threading.Event()
        self._threads = [
            threading.Thread(target=self._receive, daemon=True, name="mx-eye receive"),
            threading.Thread(
                target=self._synchronize, daemon=True, name="mx-eye clock"
            ),
        ]
        for t in self._threads:
            t.start()
        if not self._ready.wait(self.timeout):
            self.close()
            raise TimeoutError("Receiver socket did not become ready")
        if self._stats["error"]:
            error = self._stats["error"]
            self.close()
            raise RuntimeError(error)
        return self

    def _request_status(self, command: Command, timeout=None) -> StatusSnapshot:
        reply = self._rpc(Request(command=command), timeout=timeout)
        if reply.status is None:
            raise ValueError("Expected a status response")
        return reply.status

    def start(self) -> StatusSnapshot:
        self.connect()
        return self._request_status(Command.START, timeout=max(20, self.timeout))

    def stop(self) -> StatusSnapshot:
        """Stops acquisition; status() reports when video draining is complete."""
        return self._request_status(Command.STOP)

    def status(self) -> StatusSnapshot:
        return self._request_status(Command.STATUS)

    def latest(self, max_age_ms=50.0, require_valid=True):
        with self._lock:
            sample = self._latest
        if sample is None or (require_valid and not sample.frame.payload.valid):
            return None
        if max_age_ms is not None:
            age = sample.age_ms
            if (
                not math.isfinite(age)
                or age < -sample.sync_rtt_ms / 2
                or age > max_age_ms
            ):
                return None
        return sample

    def drain(self):
        """Return and clear buffered samples. Receiving never waits for this."""
        with self._lock:
            samples = list(self._samples)
            self._samples.clear()
        return samples

    @property
    def stats(self):
        with self._lock:
            out = dict(self._stats)
            out["clock_synced"] = time.perf_counter_ns() < self._clock[1]
        return out

    def _receive(self):
        sock = None
        try:
            pending = bytearray()
            if self.transport is Transport.UDP:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                sock.bind((self.udp_bind, self.data_port))
                sock.settimeout(0.1)
            self._ready.set()
            while not self._stop.is_set():
                if self.transport is Transport.TCP:
                    if sock is None:
                        try:
                            sock = socket.create_connection(
                                (self.host, self.data_port), 0.3
                            )
                            sock.settimeout(0.1)
                            pending.clear()
                        except OSError:
                            sock = None
                            self._stop.wait(0.1)
                            continue
                    size = None
                    if len(pending) >= DataFrame.header_size:
                        try:
                            _, _, length = decode_header(
                                bytes(pending[: DataFrame.header_size])
                            )
                            size = DataFrame.header_size + length
                        except ValueError:
                            with self._lock:
                                self._stats["malformed"] += 1
                            # An invalid header loses the stream boundary.
                            sock.close()
                            sock = None
                            pending.clear()
                            continue
                    if size is None or len(pending) < size:
                        try:
                            chunk = sock.recv(65536)
                            if not chunk:
                                raise ConnectionError("Data stream closed")
                            pending.extend(chunk)
                        except TimeoutError:
                            continue
                        except OSError:
                            sock.close()
                            sock = None
                            pending.clear()
                        continue
                    data = bytes(pending[:size])
                    del pending[:size]
                else:
                    try:
                        data, _ = sock.recvfrom(2048)
                    except TimeoutError:
                        continue
                received = time.perf_counter_ns()
                try:
                    data_frame = decode_frame(data)
                except ValueError:
                    with self._lock:
                        self._stats["malformed"] += 1
                    continue
                with self._lock:
                    session, seq, frame = (
                        data_frame.payload.session,
                        data_frame.payload.sequence,
                        data_frame.payload.frame,
                    )
                    if session != self._session:
                        if session in self._old_sessions:
                            continue
                        if self._session is not None:
                            self._old_sessions.append(self._session)
                        self._session = session
                        self._sequence = self._frame = None
                        self._samples.clear()
                    if self._sequence is not None:
                        if seq <= self._sequence:
                            self._stats["out_of_order"] += 1
                            continue
                        self._stats["sequence_gaps"] += seq - self._sequence - 1
                        # This includes unavailable packets as well as skipped capture frames.
                        self._stats["acquisition_skips"] += max(
                            0, frame - self._frame - (seq - self._sequence)
                        )
                    self._sequence, self._frame = seq, frame
                    offset, expiry, rtt = self._clock
                    sample = Sample(
                        frame=data_frame,
                        receive_ns=received,
                        clock_offset_ns=offset,
                        clock_valid_until_ns=expiry,
                        sync_rtt_ms=rtt,
                    )
                    if len(self._samples) == self._samples.maxlen:
                        self._stats["buffer_overwrites"] += 1
                    self._samples.append(sample)
                    self._latest = sample
                    self._stats["received"] += 1
        except Exception as exc:
            with self._lock:
                self._stats["error"] = str(exc)
            self._ready.set()
        finally:
            if sock is not None:
                sock.close()

    def _synchronize(self):
        while not self._stop.is_set():
            probes = []
            # Use the smallest-RTT probe to reduce queuing bias.
            for _ in range(8):
                if self._stop.is_set():
                    break
                try:
                    with socket.create_connection(
                        (self.host, self.control_port), 0.3
                    ) as sock:
                        sock.settimeout(0.3)
                        t1 = time.perf_counter_ns()
                        send_json(sock, Request(command=Command.SYNC, t1=t1))
                        reply = receive_json(sock, Reply)
                        t4 = time.perf_counter_ns()
                        if reply.ok and reply.sync is not None and reply.sync.t1 == t1:
                            t2, t3 = reply.sync.t2, reply.sync.t3
                            rtt = (t4 - t1) - (t3 - t2)
                            if 0 <= rtt < 300_000_000:
                                probes.append((rtt, ((t2 - t1) + (t3 - t4)) / 2))
                except (OSError, ValueError, KeyError):
                    pass
            if probes:
                rtt, offset = min(probes)
                with self._lock:
                    self._clock = (
                        offset,
                        time.perf_counter_ns() + 45_000_000_000,
                        rtt / 1e6,
                    )
                    self._stats["sync_rtt_ms"] = rtt / 1e6
            self._stop.wait(15)

    def close(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=4)
        self._threads.clear()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.close()
