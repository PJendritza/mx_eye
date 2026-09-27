"""Control-plane access and four-timestamp clock synchronization.

One request/response per connection on the tracker command port; the same
endpoint serves status, start/stop and SYNC. Cross-host age is estimated with a
four-timestamp exchange, never by comparing raw clocks.
"""

import math
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from mx_eye_protocol.control import Command, Reply, Request, StatusSnapshot
from mx_eye_protocol.json_io import receive_json, send_json


@dataclass(frozen=True)
class ClockState:
    """The tracker clock as the receiver currently estimates it.

    Read once per received frame, so this stays a dataclass rather than a
    validated model. The defaults mean "never synchronized".
    """

    offset_ns: float = math.nan  # Server minus receiver.
    valid_until_ns: int = 0  # Receiver timestamp until which the offset holds.
    rtt_ms: float = math.nan  # Smallest round trip the offset was derived from.


Connector = Callable[[tuple[str, int], float], socket.socket]


class ControlClient:
    """One request per connection against the tracker command endpoint."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        *,
        now: Callable[[], int] = time.perf_counter_ns,
        connect: Connector = socket.create_connection,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._now = now
        self._connect = connect

    def rpc(self, request: Request, timeout: float | None = None) -> Reply:
        """Send one typed request and return its validated reply."""
        timeout = self.timeout if timeout is None else timeout
        try:
            with self._connect((self.host, self.port), timeout) as sock:
                sock.settimeout(timeout)
                send_json(sock, request)
                reply = receive_json(sock, Reply)
                if not reply.ok:
                    raise RuntimeError(reply.error or "Tracker rejected command")
                return reply
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TimeoutError(
                f"Tracker did not reply at {self.host}:{self.port}: {exc}"
            ) from exc

    def request_status(
        self, command: Command, timeout: float | None = None
    ) -> StatusSnapshot:
        reply = self.rpc(Request(command=command), timeout=timeout)
        if reply.status is None:
            raise ValueError("Expected a status response")
        return reply.status

    def probe_timestamps(self, timeout: float) -> tuple[int, int, int, int] | None:
        """Four-timestamp SYNC exchange; t1 is the client send time, t4 the receipt.

        Returns None for any unusable probe: an unavailable peer, a malformed
        reply, or a reply that does not echo t1.
        """
        try:
            with self._connect((self.host, self.port), timeout) as sock:
                sock.settimeout(timeout)
                t1 = self._now()
                send_json(sock, Request(command=Command.SYNC, t1=t1))
                reply = receive_json(sock, Reply)
                t4 = self._now()
                if not reply.ok or reply.sync is None or reply.sync.t1 != t1:
                    return None
                return t1, reply.sync.t2, reply.sync.t3, t4
        except (OSError, ValueError, KeyError):
            return None


class ClockSync:
    """Background estimator of the tracker's clock relative to this receiver."""

    def __init__(
        self,
        control: ControlClient,
        *,
        now: Callable[[], int] = time.perf_counter_ns,
        probes: int = 8,
        probe_timeout: float = 0.3,
        interval_s: float = 15.0,
        validity_s: float = 45.0,
        max_rtt_ns: int = 300_000_000,
    ) -> None:
        self._control = control
        self._now = now
        self._probes = probes
        self._probe_timeout = probe_timeout
        self._interval_s = interval_s
        self._validity_ns = int(validity_s * 1e9)
        self._max_rtt_ns = max_rtt_ns
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = ClockState()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, daemon=True, name="mx-eye clock"
        )
        self._thread.start()

    def run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self._interval_s)

    def run_once(self) -> bool:
        """Take ``probes`` samples, keep the smallest round trip, refresh the state."""
        measured: list[tuple[int, float]] = []
        for _ in range(self._probes):
            if self._stop.is_set():
                break
            probe = self._probe()
            if probe is not None:
                measured.append(probe)
        if not measured:
            return False
        # Use the smallest-RTT probe to reduce queuing bias.
        rtt, offset = min(measured)
        with self._lock:
            self._state = ClockState(
                offset_ns=offset,
                valid_until_ns=self._now() + self._validity_ns,
                rtt_ms=rtt / 1e6,
            )
        return True

    def state(self) -> ClockState:
        with self._lock:
            return self._state

    @property
    def rtt_ms(self) -> float:
        return self.state().rtt_ms

    @property
    def synced(self) -> bool:
        state = self.state()
        return math.isfinite(state.offset_ns) and self._now() < state.valid_until_ns

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=4)

    def _probe(self) -> tuple[int, float] | None:
        stamps = self._control.probe_timestamps(self._probe_timeout)
        if stamps is None:
            return None
        t1, t2, t3, t4 = stamps
        rtt = (t4 - t1) - (t3 - t2)
        if not 0 <= rtt < self._max_rtt_ns:
            return None
        return rtt, ((t2 - t1) + (t3 - t4)) / 2
