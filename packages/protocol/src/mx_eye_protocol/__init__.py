"""Shared wire formats between the mx-eye tracker and the mx-eye SDK.

This package holds tracking data models, their binary codec,
and the control-plane messages. Its models use Pydantic, and this package
must not import Qt, OpenCV, or the tracker.
"""

from .control import (
    Command,
    NetworkStatus,
    Reply,
    Request,
    SourceMode,
    SourceStatus,
    StatusSnapshot,
    TrackingStats,
    Transport,
)
from .data_frame import (
    TRACKING_FIELDS,
    DataFrame,
    MessageType,
    TrackingFlags,
    TrackingPayload,
)

__version__ = "0.1.0"
__all__ = [
    "TRACKING_FIELDS",
    "Command",
    "DataFrame",
    "MessageType",
    "NetworkStatus",
    "Reply",
    "Request",
    "SourceMode",
    "SourceStatus",
    "StatusSnapshot",
    "TrackingFlags",
    "TrackingPayload",
    "TrackingStats",
    "Transport",
]
