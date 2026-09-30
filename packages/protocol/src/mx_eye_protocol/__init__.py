"""Shared wire formats between the mx-eye tracker and the mx-eye SDK.

This package holds tracking data models, their binary codec,
and the control-plane messages. Its models use Pydantic, and this package
must not import Qt, OpenCV, or the tracker.
"""

from .control import (
    Command,
    ControlReply,
    ControlRequest,
    NetworkStatus,
    SourceMode,
    SourceStatus,
    StatusSnapshot,
    TrackingStats,
)
from .data_frame import (
    TRACKING_FIELDS,
    DataFrame,
    TrackingFlags,
)

__version__ = "0.1.0"
__all__ = [
    "TRACKING_FIELDS",
    "Command",
    "ControlReply",
    "ControlRequest",
    "DataFrame",
    "NetworkStatus",
    "SourceMode",
    "SourceStatus",
    "StatusSnapshot",
    "TrackingFlags",
    "TrackingStats",
]
