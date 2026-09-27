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
from .packets import (
    CR,
    MAGIC,
    PACKET,
    PUPIL,
    PUPIL_ONLY,
    ROI_RELATIVE,
    SIMULATION,
    VALID,
    VERSION,
    Packet,
    decode,
    encode,
)

__version__ = "0.1.0"
__all__ = [
    "CMD_START",
    "CMD_STATUS",
    "CMD_STOP",
    "CMD_SYNC",
    "CONTROL_COMMANDS",
    "CR",
    "MAGIC",
    "PACKET",
    "PROTOCOL_VERSION",
    "PUPIL",
    "PUPIL_ONLY",
    "ROI_RELATIVE",
    "SIMULATION",
    "SYNC_COMMANDS",
    "VALID",
    "VERSION",
    "Packet",
    "Reply",
    "Request",
    "StatusSnapshot",
    "decode",
    "encode",
]
