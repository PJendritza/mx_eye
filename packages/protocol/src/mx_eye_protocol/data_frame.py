"""Tracking payload and shared message frame models."""

import math
import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag, auto
from typing import ClassVar


class MessageType(IntEnum):
    """Message kinds in the shared frame header; CMD is reserved for control."""

    # Member order is part of the binary wire contract.
    DATA = auto()
    CMD = auto()


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


@dataclass(frozen=True)
class TrackingPayload:
    """One tracking sample, independent of its serialized representation.

    Timestamps except media_ns use the tracker's monotonic clock, not UTC.
    Coordinates are uncalibrated source-image pixels: x rightward, y downward.
    Missing detections and invalid output coordinates are represented by NaN.
    """

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


@dataclass(frozen=True, kw_only=True)
class DataFrame:
    """A message envelope; length counts encoded payload bytes, not the header.

    Only DATA payloads are implemented. Control messages will reuse this
    envelope when their codec is introduced.
    """

    magic: bytes = b"MXEY"  # Four-byte marker identifying the frame protocol.
    message_type: MessageType  # DATA for tracking; CMD reserved for control.
    length: int  # Encoded payload byte count, excluding the header.
    payload: TrackingPayload  # Typed payload before binary serialization.

    # Little endian: magic, uint8 message type, uint32 encoded payload length.
    HEADER: ClassVar[struct.Struct] = struct.Struct("<4sBI")
    # Seven uint64 values, signed media time, eight float32 values, uint8 flags.
    TRACKING: ClassVar[struct.Struct] = struct.Struct("<7Qq8fB")
    header_size: ClassVar[int] = HEADER.size

    @classmethod
    def from_payload(cls, payload: TrackingPayload) -> "DataFrame":
        """Create a DATA frame with the binary payload's encoded byte length."""
        return cls(
            message_type=MessageType.DATA,
            length=cls.TRACKING.size,
            payload=payload,
        )

    def encode(self) -> bytes:
        """Serialize the frame fields without receiver-side protocol validation."""
        header = self.HEADER.pack(self.magic, self.message_type, self.length)
        payload = self.payload
        body = self.TRACKING.pack(
            payload.session,
            payload.sequence,
            payload.frame,
            payload.acquisition_ns,
            payload.tracking_start_ns,
            payload.tracking_end_ns,
            payload.send_ns,
            payload.media_ns,
            payload.x,
            payload.y,
            payload.pupil_x,
            payload.pupil_y,
            payload.cr_x,
            payload.cr_y,
            payload.pupil_area,
            payload.template_ncc,
            int(payload.flags),
        )
        return header + body

    @property
    def frame_size(self) -> int:
        """Total frame byte count: header plus declared payload length."""
        return self.header_size + self.length
