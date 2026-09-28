"""Byte-at-a-time framing: one frame out, malformed headers, recovery."""

import struct

import pytest
from mx_eye_protocol import DataFrame, MessageType, TrackingFlags, TrackingPayload
from py_mx_eye.receiver import FrameAssembler, FrameError

HEADER = struct.Struct("<4sBI")
FRAME_BYTES = 106


def _payload(sequence: int) -> TrackingPayload:
    return TrackingPayload(
        session=1,
        sequence=sequence,
        frame=sequence,
        acquisition_ns=1,
        tracking_start_ns=2,
        tracking_end_ns=3,
        send_ns=4,
        media_ns=-1,
        x=1.0,
        y=2.0,
        pupil_x=3.0,
        pupil_y=4.0,
        cr_x=5.0,
        cr_y=6.0,
        pupil_area=7.0,
        template_ncc=0.5,
        flags=TrackingFlags.VALID,
    )


def _frames(count: int) -> list[bytes]:
    return [
        DataFrame.from_payload(_payload(sequence)).encode()
        for sequence in range(1, count + 1)
    ]


def _run(assembler: FrameAssembler, chunk: bytes) -> list[bytes]:
    """Feed one chunk the way the receive loop does, collecting its frames."""
    produced: list[bytes] = []
    for byte in chunk:
        frame = assembler.feed(byte)
        if frame is not None:
            produced.append(frame)
    return produced


def test_a_frame_is_returned_only_by_the_byte_that_completes_it():
    frames = _frames(2)
    assembler = FrameAssembler()
    completed_at: list[int] = []
    for index, byte in enumerate(b"".join(frames)):
        if assembler.feed(byte) is not None:
            completed_at.append(index)
    assert completed_at == [FRAME_BYTES - 1, 2 * FRAME_BYTES - 1]


def test_a_partial_frame_is_not_returned_until_it_completes():
    frame = _frames(1)[0]
    assembler = FrameAssembler()
    assert [assembler.feed(byte) for byte in frame[:-1]] == [None] * (FRAME_BYTES - 1)
    assert assembler.feed(frame[-1]) == frame


@pytest.mark.parametrize("size", [1, 5, 10, FRAME_BYTES, 1000])
def test_the_callers_chunking_does_not_matter(size):
    stream = b"".join(_frames(3))
    assembler = FrameAssembler()
    produced: list[bytes] = []
    for start in range(0, len(stream), size):
        produced.extend(_run(assembler, stream[start : start + size]))
    assert produced == _frames(3)


def test_reset_discards_a_partial_frame():
    partial, complete = _frames(1)[0], _frames(2)[1]
    assembler = FrameAssembler()
    _run(assembler, partial[:50])
    assembler.reset()
    assert _run(assembler, complete) == [complete]


@pytest.mark.parametrize(
    "bad_header",
    [
        HEADER.pack(b"FAIL", MessageType.DATA, 97),
        HEADER.pack(b"MXEY", 255, 97),
        HEADER.pack(b"MXEY", MessageType.CMD, 97),
        HEADER.pack(b"MXEY", MessageType.DATA, 0),
        HEADER.pack(b"MXEY", MessageType.DATA, 0xFFFFFFFF),
        HEADER.pack(b"MXEY", MessageType.DATA, 0xFFFFFFF0),
    ],
)
def test_a_malformed_header_raises_and_clears_the_buffer(bad_header):
    first, second = _frames(2)
    assembler = FrameAssembler()
    with pytest.raises(FrameError):
        _run(assembler, bad_header + first)
    # The stream boundary is lost: the old buffer may not be reused, so the
    # assembler has to start again from a clean header.
    assert _run(assembler, second) == [second]


def test_a_valid_prefix_before_a_fault_is_still_delivered():
    first, second = _frames(2)
    bad_header = HEADER.pack(b"FAIL", MessageType.DATA, 97)
    assembler = FrameAssembler()
    produced: list[bytes] = []
    with pytest.raises(FrameError):
        for byte in first + bad_header + second:
            frame = assembler.feed(byte)
            if frame is not None:
                produced.append(frame)
    # The good frame completed before the faulty header, so it is not thrown away.
    assert produced == [first]
    assert _run(assembler, second) == [second]


def test_frame_error_is_an_error_and_a_value_error():
    assert issubclass(FrameError, ValueError)
