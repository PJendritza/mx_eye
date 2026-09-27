"""Version 1 mx_eye wire format; intentionally distinct from transport test v4."""

import struct
from dataclasses import dataclass

MAGIC = b"MXEY"
VERSION = 1
VALID = 1
PUPIL = 2
CR = 4
PUPIL_ONLY = 8
SIMULATION = 16
ROI_RELATIVE = 32
# magic/version/flags/reserved; session, sequence, frame, acquisition,
# tracking-start, tracking-end, send (all uint64); media time (signed ns);
# x,y,pupil-x,pupil-y,CR-x,CR-y,pupil-area,template-NCC (float32).
PACKET = struct.Struct("<4sBBH7Qq8f")


@dataclass(frozen=True)
class Packet:
    """One tracking sample in wire order; the header keeps magic and version."""

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
    flags: int = 0

    def body(self):
        """The 16 payload values in wire order, without header or flags."""
        return (
            self.session,
            self.sequence,
            self.frame,
            self.acquisition_ns,
            self.tracking_start_ns,
            self.tracking_end_ns,
            self.send_ns,
            self.media_ns,
            self.x,
            self.y,
            self.pupil_x,
            self.pupil_y,
            self.cr_x,
            self.cr_y,
            self.pupil_area,
            self.template_ncc,
        )


def encode(packet):
    return PACKET.pack(MAGIC, VERSION, packet.flags, 0, *packet.body())


def decode(data):
    if len(data) != PACKET.size:
        raise ValueError("Invalid mx_eye packet size")
    p = PACKET.unpack(data)
    if p[0] != MAGIC or p[1] != VERSION:
        raise ValueError("Unsupported mx_eye protocol")
    return Packet(*p[4:12], *p[12:20], flags=p[2])
