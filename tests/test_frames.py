"""Boundary tests for frame models, wire compatibility, SDK and recording."""

import csv
import io
import math
import multiprocessing
import struct
from dataclasses import replace

import pytest
from mx_eye.recording import TRACKING_COLUMNS, tracking_row
from mx_eye_protocol import (
    TRACKING_FIELDS,
    DataFrame,
    MessageType,
    TrackingFlags,
    TrackingPayload,
)
from py_mx_eye import Sample
from py_mx_eye.receiver import decode_frame, decode_header

HEADER = struct.Struct("<4sBI")

# Header: MXEY, DATA=1, length=97. Payload: seven uint64, signed -1,
# eight float32 values and flags=63. Independent of the implementation's Structs.
FRAME_BYTES = bytes.fromhex(
    "4d5845590161000000"
    "0100000000000000"
    "0200000000000000"
    "0300000000000000"
    "0400000000000000"
    "0500000000000000"
    "0600000000000000"
    "0700000000000000"
    "ffffffffffffffff"
    "0000803f0000004000004040000080400000a0400000c0400000e040000000413f"
)


@pytest.fixture
def payload():
    return TrackingPayload(
        session=1,
        sequence=2,
        frame=3,
        acquisition_ns=4,
        tracking_start_ns=5,
        tracking_end_ns=6,
        send_ns=7,
        media_ns=-1,
        x=1.0,
        y=2.0,
        pupil_x=3.0,
        pupil_y=4.0,
        cr_x=5.0,
        cr_y=6.0,
        pupil_area=7.0,
        template_ncc=8.0,
        flags=TrackingFlags(63),
    )


def test_fixed_bytes(payload):
    assert len(FRAME_BYTES) == 106
    outgoing = DataFrame.from_payload(payload)
    assert outgoing.payload is payload
    assert outgoing.encode() == FRAME_BYTES
    frame = decode_frame(FRAME_BYTES)
    assert frame == outgoing
    assert frame.encode() == FRAME_BYTES
    assert frame == DataFrame(message_type=MessageType.DATA, length=97, payload=payload)
    assert outgoing.frame_size == len(FRAME_BYTES)


def test_wire_field_order_is_declared_once():
    """Model order, decode field list and struct arity share one declaration."""
    assert TRACKING_FIELDS == tuple(TrackingPayload.model_fields)
    assert DataFrame.TRACKING.size == 97
    # The strict zip in decode_frame relies on these two counts agreeing.
    assert len(DataFrame.TRACKING.unpack(bytes(DataFrame.TRACKING.size))) == len(
        TRACKING_FIELDS
    )


def test_auto_enum_wire_values():
    assert (int(MessageType.DATA), int(MessageType.CMD)) == (1, 2)
    assert (
        int(TrackingFlags.NONE),
        int(TrackingFlags.VALID),
        int(TrackingFlags.PUPIL),
        int(TrackingFlags.CR),
        int(TrackingFlags.PUPIL_ONLY),
        int(TrackingFlags.SIMULATION),
        int(TrackingFlags.ROI_RELATIVE),
    ) == (0, 1, 2, 4, 8, 16, 32)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        FRAME_BYTES[:8],
        FRAME_BYTES[:-1],
        FRAME_BYTES + b"\x00",
        b"FAIL" + FRAME_BYTES[4:],
        FRAME_BYTES[:4] + b"\x00" + FRAME_BYTES[5:],
        FRAME_BYTES[:4] + b"\xff" + FRAME_BYTES[5:],
        FRAME_BYTES[:5] + bytes.fromhex("00000000") + FRAME_BYTES[9:],
        FRAME_BYTES[:5] + bytes.fromhex("60000000") + FRAME_BYTES[9:],
        FRAME_BYTES[:5] + bytes.fromhex("62000000") + FRAME_BYTES[9:],
        FRAME_BYTES[:5] + bytes.fromhex("ffffffff") + FRAME_BYTES[9:],
    ],
)
def test_malformed_frame(data):
    with pytest.raises(ValueError):
        decode_frame(data)


@pytest.mark.parametrize(
    "header",
    [
        b"",
        FRAME_BYTES[:8],
        FRAME_BYTES[:10],
        b"FAIL" + FRAME_BYTES[4:9],
        FRAME_BYTES[:4] + b"\xff" + FRAME_BYTES[5:9],
        FRAME_BYTES[:5] + bytes.fromhex("ffffffff"),
    ],
)
def test_invalid_headers_rejected_before_body(header):
    with pytest.raises(ValueError):
        decode_header(header)


def test_cmd_reserved():
    header = HEADER.pack(b"MXEY", MessageType.CMD, 0)
    with pytest.raises(ValueError, match="CMD payloads are not implemented"):
        decode_header(header)
    with pytest.raises(ValueError, match="CMD payloads are not implemented"):
        decode_frame(header)


def test_old_v1_frame_rejected():
    legacy = bytes.fromhex("4d584559013f0000") + FRAME_BYTES[9:-1]
    assert len(legacy) == 104
    with pytest.raises(ValueError):
        decode_frame(legacy)


def test_float32_and_missing_values(payload):
    decoded = decode_frame(
        DataFrame.from_payload(
            payload.model_copy(
                update={"x": 0.1, "cr_x": math.nan, "flags": TrackingFlags.VALID}
            )
        ).encode()
    ).payload
    assert decoded.x == 0.10000000149011612
    assert math.isnan(decoded.cr_x)
    assert decoded.flags is TrackingFlags.VALID


@pytest.mark.parametrize(
    ("flags", "coordinates"),
    [
        (TrackingFlags.NONE, "pupil_minus_cr"),
        (TrackingFlags.ROI_RELATIVE, "pupil_minus_cr"),
        (TrackingFlags.PUPIL_ONLY, "image_absolute"),
        (TrackingFlags.PUPIL_ONLY | TrackingFlags.ROI_RELATIVE, "roi_relative"),
    ],
)
def test_coordinates(payload, flags, coordinates):
    assert payload.model_copy(update={"flags": flags}).coordinate_system == coordinates


def test_measurements_and_validity(payload):
    frame = payload.model_copy(
        update={
            "acquisition_ns": 1_000_000,
            "tracking_start_ns": 2_000_000,
            "tracking_end_ns": 4_000_000,
            "send_ns": 5_000_000,
        }
    )
    assert frame.valid
    assert frame.queue_ms == 1
    assert frame.processing_ms == 2
    assert frame.acquisition_to_send_ms == 4
    assert not frame.model_copy(update={"flags": TrackingFlags.NONE}).valid
    assert not frame.model_copy(update={"x": math.nan}).valid
    assert not frame.model_copy(update={"y": math.inf}).valid


def _timed_sample(payload):
    payload = payload.model_copy(
        update={"acquisition_ns": 2_000_000, "send_ns": 5_000_000}
    )
    frame = DataFrame(message_type=MessageType.DATA, length=97, payload=payload)
    return Sample(frame=frame, receive_ns=7_000_000)


def test_sample_timing(payload):
    sample = _timed_sample(payload)
    assert sample.frame.payload.send_ns == 5_000_000
    # Delay compares the frame timestamps with receiver time directly.
    assert sample.network_ms == 2
    assert sample.arrival_age_ms == 5
    assert sample.age_ms_at(10_000_000) == 8
    # The property reads the process clock, which is far ahead of these stamps.
    assert sample.age_ms > 0


def test_sample_freshness(payload):
    sample = _timed_sample(payload)
    # Age at 10 ms is 8 ms, so the 10 ms budget passes and the 7 ms budget does not.
    assert sample.is_fresh_at(10_000_000, max_age_ms=10)
    assert not sample.is_fresh_at(10_000_000, max_age_ms=7)
    # Zero age is the boundary of a shared-clock comparison.
    assert sample.is_fresh_at(2_000_000, max_age_ms=0)
    # None skips the age check only, not validity.
    assert sample.is_fresh_at(20_000_001, max_age_ms=None)
    lost = replace(
        sample,
        frame=replace(
            sample.frame,
            payload=sample.frame.payload.model_copy(
                update={"flags": TrackingFlags.NONE}
            ),
        ),
    )
    assert not lost.is_fresh_at(10_000_000, max_age_ms=10)
    assert lost.is_fresh_at(10_000_000, max_age_ms=10, require_valid=False)
    # A timestamp ahead of the receiver clock means the ends disagree, so the
    # sample is not certified fresh; diagnostics can still read its age.
    assert sample.age_ms_at(1_000_000) == -1
    assert not sample.is_fresh_at(1_000_000, max_age_ms=50)
    assert sample.is_fresh_at(1_000_000, max_age_ms=None)


def _send_frame(queue, frame):
    queue.put(frame)


def test_payload_cross_process_and_csv(payload):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_send_frame, args=(queue, payload))
    process.start()
    try:
        received = queue.get(timeout=10)
        process.join(timeout=10)
        assert process.exitcode == 0
        assert received == payload
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        queue.close()
        queue.join_thread()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(TRACKING_COLUMNS)
    writer.writerow(tracking_row(received))
    assert output.getvalue().splitlines() == [
        (
            "session,sequence,source_frame,acquisition_ns,tracking_start_ns,"
            "tracking_end_ns,send_ns,media_ns,x,y,pupil_x,pupil_y,cr_x,cr_y,"
            "pupil_area,template_ncc,flags"
        ),
        "1,2,3,4,5,6,7,-1,1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0,63",
    ]
    precise = payload.model_copy(update={"x": 0.1, "cr_x": math.nan})
    row = dict(zip(TRACKING_COLUMNS, tracking_row(precise)))
    assert row["x"] == 0.1
    assert math.isnan(row["cr_x"])
