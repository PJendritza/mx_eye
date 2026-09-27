"""Received sample and its pure receiver-local timing arithmetic.

The ``*_at`` methods take the caller's monotonic timestamp so freshness and age
can be evaluated deterministically; the properties use ``perf_counter_ns``.
"""

import math
import time
from dataclasses import dataclass

from mx_eye_protocol.data_frame import DataFrame


@dataclass(frozen=True)
class Sample:
    """A received DATA frame with receiver-local timing information."""

    frame: DataFrame
    receive_ns: int
    clock_offset_ns: float = math.nan  # server minus receiver
    clock_valid_until_ns: int = 0
    sync_rtt_ms: float = math.nan

    def clock_valid_at(self, now_ns: int) -> bool:
        """Whether the clock offset was usable at ``now_ns``."""
        return (
            math.isfinite(self.clock_offset_ns) and now_ns <= self.clock_valid_until_ns
        )

    @property
    def clock_valid(self) -> bool:
        return self.clock_valid_at(time.perf_counter_ns())

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

    def age_ms_at(self, now_ns: int) -> float:
        """Age measured at ``now_ns``, including time spent in the caller's code."""
        return (
            (now_ns - self.frame.payload.acquisition_ns + self.clock_offset_ns) / 1e6
            if self.clock_valid_at(now_ns)
            else math.nan
        )

    @property
    def age_ms(self) -> float:
        """Age now, including time the sample has waited in the user's code."""
        return self.age_ms_at(time.perf_counter_ns())

    def is_fresh_at(
        self,
        now_ns: int,
        max_age_ms: float | None,
        require_valid: bool = True,
    ) -> bool:
        """Whether a consumer may treat this sample as a current measurement.

        ``max_age_ms=None`` skips the age check entirely, which is intended for
        diagnostics. A negative age within half the synchronization round trip is
        accepted as ordinary clock ambiguity.
        """
        if require_valid and not self.frame.payload.valid:
            return False
        if max_age_ms is None:
            return True
        age = self.age_ms_at(now_ns)
        return math.isfinite(age) and not (
            age < -self.sync_rtt_ms / 2 or age > max_age_ms
        )
