"""Tracking frame model and shared binary layout."""

import math
import struct
from enum import IntFlag, auto
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class TrackingFlags(IntFlag):
    """Combinable tracking status, output-mode and source flags."""

    # Zero means no flags; auto() allocates successive bits for other members.
    NONE = 0
    VALID = auto()  # The output x/y signal is valid.
    PUPIL = auto()  # A pupil detection is available.
    CR = auto()  # A corneal-reflection detection is available.
    PUPIL_ONLY = auto()  # Output pupil coordinates instead of pupil minus CR.
    SIMULATION = auto()  # The source is synthetic.
    ROI_RELATIVE = (
        auto()
    )  # Pupil-only x/y use the ROI origin instead of the image origin.


# Tracking wire order: every payload field with its struct code, in byte order.
# The struct format, its size and the decode field list all derive from this one
# declaration, so the codec cannot disagree with itself. Little endian: seven uint64, signed media time, eight float32, uint8 flags.
_TRACKING_LAYOUT: tuple[tuple[str, str], ...] = (
    ("session", "Q"),
    ("sequence", "Q"),
    ("frame", "Q"),
    ("acquisition_ns", "Q"),
    ("tracking_start_ns", "Q"),
    ("tracking_end_ns", "Q"),
    ("send_ns", "Q"),
    ("media_ns", "q"),
    ("x", "f"),
    ("y", "f"),
    ("pupil_x", "f"),
    ("pupil_y", "f"),
    ("cr_x", "f"),
    ("cr_y", "f"),
    ("pupil_area", "f"),
    ("template_ncc", "f"),
    ("flags", "B"),
)

# Payload field names in wire order, for a caller decoding a payload itself.
TRACKING_FIELDS: tuple[str, ...] = tuple(name for name, _ in _TRACKING_LAYOUT)

_TRACKING_FORMAT = "<" + "".join(code for _, code in _TRACKING_LAYOUT)


class DataFrame(BaseModel):
    """One tracking sample, independent of its serialized representation.

    Timestamps except media_ns are nanoseconds from the shared wall clock
    (CLOCK_REALTIME, Unix epoch), aligned between hosts by system-level NTP or
    PTP; they are not a monotonic uptime clock. media_ns is a media position.
    Coordinates are uncalibrated source-image pixels: x rightward, y downward.
    Missing detections and invalid output coordinates are represented by NaN.

    Field order is the wire order; the declared types decode a payload, so for
    example the raw flags byte becomes a TrackingFlags member.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    session: int  # Random 64-bit ID generated when a tracking session starts.
    sequence: int  # 1-based tracking-sample counter within the session.
    frame: int  # 1-based acquired-frame ID; can skip between tracking samples.
    acquisition_ns: int  # Host read-return time; excludes camera/USB latency.
    tracking_start_ns: int  # Time immediately before processing this frame.
    tracking_end_ns: int  # Time immediately after processing this frame.
    send_ns: int  # Time before encoding/sending, not transmission completion.
    media_ns: int  # Video/simulation position in ns; -1 for camera input.
    # Pupil minus CR, or pupil coordinates in the origin selected by flags.
    x: float
    y: float
    # Absolute pupil center in the full image, regardless of output origin.
    pupil_x: float
    pupil_y: float
    # Absolute corneal-reflection (CR) center in the full image.
    cr_x: float
    cr_y: float
    pupil_area: float  # Detected pupil component area in square pixels.
    template_ncc: float  # Normalized template correlation; NaN if unavailable.
    flags: TrackingFlags = TrackingFlags.NONE  # Combine members with bitwise OR.

    @property
    def valid(self) -> bool:
        return (
            bool(self.flags & TrackingFlags.VALID)
            and math.isfinite(self.x)
            and math.isfinite(self.y)
        )

    @property
    def coordinate_system(self) -> str:
        if not self.flags & TrackingFlags.PUPIL_ONLY:
            return "pupil_minus_cr"
        return (
            "roi_relative"
            if self.flags & TrackingFlags.ROI_RELATIVE
            else "image_absolute"
        )

    @property
    def processing_ms(self) -> float:
        return (self.tracking_end_ns - self.tracking_start_ns) / 1e6

    @property
    def acquisition_to_send_ms(self) -> float:
        return (self.send_ns - self.acquisition_ns) / 1e6

    @property
    def queue_ms(self) -> float:
        return (self.tracking_start_ns - self.acquisition_ns) / 1e6

    TRACKING: ClassVar[struct.Struct] = struct.Struct(_TRACKING_FORMAT)

    def encode(self) -> bytes:
        """Serialize this frame; ZeroMQ supplies message boundaries."""
        return self.TRACKING.pack(*(getattr(self, name) for name in TRACKING_FIELDS))

    @property
    def frame_size(self) -> int:
        return self.TRACKING.size
