"""Shared wire formats between the mx-eye tracker and the mx-eye SDK.

This package holds the versioned wire contract only: the binary packet layout
and the control-plane messages. It depends on the standard library alone and
must not import Qt, OpenCV, or the tracker.
"""

from .control import (
    CMD_START,
    CMD_STATUS,
    CMD_STOP,
    CMD_SYNC,
    CONTROL_COMMANDS,
    PROTOCOL_VERSION,
    SYNC_COMMANDS,
    Reply,
    Request,
    StatusSnapshot,
)
from .data_frame import (
    DataFrame,
    MessageType,
    TrackingFlags,
    TrackingPayload,
)

__version__ = "0.1.0"
__all__ = [
    "DataFrame",
    "MessageType",
    "TrackingFlags",
    "TrackingPayload",
    "CMD_START",
    "CMD_STATUS",
    "CMD_STOP",
    "CMD_SYNC",
    "CONTROL_COMMANDS",
    "PROTOCOL_VERSION",
    "SYNC_COMMANDS",
    "Reply",
    "Request",
    "StatusSnapshot",
]
