"""Small thread-based receiver SDK; no Qt/OpenCV dependency.

Client receives continuously even when the caller is busy drawing. Cross-host
age is estimated with a four-timestamp exchange, not by comparing raw clocks.
"""

import json
import math
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

from mx_eye_protocol.control import (
    CMD_START,
    CMD_STATUS,
    CMD_STOP,
    CMD_SYNC,
    Reply,
    Request,
)
from mx_eye_protocol.packets import PACKET, PUPIL_ONLY, ROI_RELATIVE, VALID, decode


# Client-side copy of the framed-JSON control helpers; the tracker keeps its own
# implementation in mx_eye/transport.py.
def receive_json(sock):
    data = bytearray()
    while b"\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Peer closed before replying")
        data.extend(chunk)
        if len(data) > 16384:
            raise ValueError("Control message is too large")
    return json.loads(data.split(b"\n", 1)[0])


def send_json(sock, obj):
    sock.sendall(json.dumps(obj, allow_nan=False).encode("utf-8") + b"\n")


@dataclass(frozen=True)
class Sample:
    session: int
    sequence: int
    frame: int
    acquisition_ns: int
    tracking_start_ns: int
    tracking_end_ns: int
    send_ns: int
    media_ns: int
    x: float
    y: float
    pupil_x: float
    pupil_y: float
    cr_x: float
    cr_y: float
    pupil_area: float
    template_ncc: float
    flags: int
    receive_ns: int
    clock_offset_ns: float = math.nan  # server minus receiver
    clock_valid_until_ns: int = 0
    sync_rtt_ms: float = math.nan

    @classmethod
    def from_packet(
        cls,
        packet,
        receive_ns,
        clock_offset_ns=math.nan,
        clock_valid_until_ns=0,
        sync_rtt_ms=math.nan,
    ):
        return cls(
            *packet.body(),
            packet.flags,
            receive_ns,
            clock_offset_ns,
            clock_valid_until_ns,
            sync_rtt_ms,
        )

    @property
    def valid(self):
        return (
            bool(self.flags & VALID) and math.isfinite(self.x) and math.isfinite(self.y)
        )

    @property
    def coordinate_system(self):
        if not self.flags & PUPIL_ONLY:
            return "pupil_minus_cr"
        return "roi_relative" if self.flags & ROI_RELATIVE else "image_absolute"

    @property
    def processing_ms(self):
        return (self.tracking_end_ns - self.tracking_start_ns) / 1e6

    @property
    def acquisition_to_send_ms(self):
        return (self.send_ns - self.acquisition_ns) / 1e6

    @property
    def queue_ms(self):
        return (self.tracking_start_ns - self.acquisition_ns) / 1e6

    @property
    def clock_valid(self):
        return (
            math.isfinite(self.clock_offset_ns)
            and time.perf_counter_ns() <= self.clock_valid_until_ns
        )

    @property
    def network_ms(self):
        return (
            (self.receive_ns - self.send_ns + self.clock_offset_ns) / 1e6
            if self.clock_valid
            else math.nan
        )

    @property
    def arrival_age_ms(self):
        return (
            (self.receive_ns - self.acquisition_ns + self.clock_offset_ns) / 1e6
            if self.clock_valid
            else math.nan
        )

    @property
    def age_ms(self):
        """Age now, including time the sample has waited in the user's code."""
        return (
            (time.perf_counter_ns() - self.acquisition_ns + self.clock_offset_ns) / 1e6
            if self.clock_valid
            else math.nan
        )


class Client:
    def __init__(
        self,
        host="127.0.0.1",
        data_port=5556,
        control_port=5557,
        sync_port=5558,
        transport=None,
        udp_bind="0.0.0.0",
        buffer_samples=4096,
        timeout=3.0,
    ):
        self.host, self.data_port = host, data_port
        self.control_port, self.sync_port = control_port, sync_port
        self.transport, self.udp_bind = transport, udp_bind
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

    def _rpc(self, request, port=None, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        try:
            with socket.create_connection(
                (self.host, port or self.control_port), timeout
            ) as sock:
                sock.settimeout(timeout)
                send_json(sock, request.to_dict())
                reply = Reply.from_dict(receive_json(sock))
                if not reply.ok:
                    raise RuntimeError(reply.error or "Tracker rejected command")
                return reply
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TimeoutError(
                f"Tracker did not reply at {self.host}:{port or self.control_port}: {exc}"
            ) from exc

    def connect(self):
        if self._threads:
            return self
        status = self.status()
        self._stats["error"] = ""
        self.transport = self.transport or status["network"]["transport"]
        if self.transport not in ("tcp", "udp"):
            raise ValueError("Transport must be tcp or udp")
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

    def start(self):
        self.connect()
        return self._rpc(Request(CMD_START), timeout=max(20, self.timeout)).to_dict()

    def stop(self):
        """Stops acquisition; status() reports when video draining is complete."""
        return self._rpc(Request(CMD_STOP)).to_dict()

    def status(self):
        return self._rpc(Request(CMD_STATUS)).to_dict()

    def latest(self, max_age_ms=50.0, require_valid=True):
        with self._lock:
            sample = self._latest
        if sample is None or (require_valid and not sample.valid):
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
            if self.transport == "udp":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                sock.bind((self.udp_bind, self.data_port))
                sock.settimeout(0.1)
            self._ready.set()
            while not self._stop.is_set():
                if self.transport == "tcp":
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
                    if len(pending) < PACKET.size:
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
                    if len(pending) < PACKET.size:
                        continue
                    data = bytes(pending[: PACKET.size])
                    del pending[: PACKET.size]
                else:
                    try:
                        data, _ = sock.recvfrom(2048)
                    except TimeoutError:
                        continue
                received = time.perf_counter_ns()
                try:
                    packet = decode(data)
                except ValueError:
                    with self._lock:
                        self._stats["malformed"] += 1
                    continue
                with self._lock:
                    session, seq, frame = packet.session, packet.sequence, packet.frame
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
                    sample = Sample.from_packet(packet, received, offset, expiry, rtt)
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
                        (self.host, self.sync_port), 0.3
                    ) as sock:
                        sock.settimeout(0.3)
                        t1 = time.perf_counter_ns()
                        send_json(sock, Request(CMD_SYNC, t1=t1).to_dict())
                        reply = Reply.from_dict(receive_json(sock))
                        t4 = time.perf_counter_ns()
                        if (
                            reply.ok
                            and reply.t1 == t1
                            and reply.t2 is not None
                            and reply.t3 is not None
                        ):
                            t2, t3 = int(reply.t2), int(reply.t3)
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
