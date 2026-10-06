"""Received sample and its pure receiver-local timing arithmetic.

The ``*_at`` methods take the caller's timestamp so freshness and age can be
evaluated deterministically; the properties read the shared wall clock.

Age and delay compare a frame timestamp with receiver time directly, so the
tracker and the receiver must read the same system clock (one host, or hosts
synchronized by system-level NTP/PTP). No application-level offset is applied.
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

    @property
    def network_ms(self) -> float:
        return (self.receive_ns - self.frame.send_ns) / 1e6

    @property
    def arrival_age_ms(self) -> float:
        return (self.receive_ns - self.frame.acquisition_ns) / 1e6

    def age_ms_at(self, now_ns: int) -> float:
        """Age measured at ``now_ns``, including time spent in the caller's code."""
        return (now_ns - self.frame.acquisition_ns) / 1e6

    @property
    def age_ms(self) -> float:
        """Age now, including time the sample has waited in the user's code."""
        return self.age_ms_at(time.time_ns())

    def is_fresh_at(
        self,
        now_ns: int,
        max_age_ms: float | None,
        require_valid: bool = True,
    ) -> bool:
        """Whether a consumer may treat this sample as a current measurement.

        ``max_age_ms=None`` skips the age check entirely, which is intended for
        diagnostics. A timestamp ahead of the receiver clock means the two ends
        are not reading the same clock, so the sample cannot be certified fresh.
        """
        if require_valid and not self.frame.valid:
            return False
        if max_age_ms is None:
            return True
        age = self.age_ms_at(now_ns)
        return math.isfinite(age) and 0.0 <= age <= max_age_ms
