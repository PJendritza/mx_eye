"""Receiver-owned binary decoding and validation for tracking frames."""

from mx_eye_protocol.data_frame import DataFrame, MessageType, TrackingFlags, TrackingPayload


def decode_header(data: bytes) -> tuple[bytes, MessageType, int]:
    """Validate an incoming DATA header before buffering its payload."""
    if len(data) != DataFrame.header_size:
        raise ValueError("Invalid mx_eye frame header size")
    magic, kind, length = DataFrame.HEADER.unpack(data)
    if magic != DataFrame.magic:
        raise ValueError("Invalid mx_eye frame magic")
    try:
        message_type = MessageType(kind)
    except ValueError as exc:
        raise ValueError("Unknown mx_eye message type") from exc
    if message_type is MessageType.CMD:
        raise ValueError("CMD payloads are not implemented")
    if length != DataFrame.TRACKING.size:
        raise ValueError("Invalid mx_eye tracking payload length")
    return magic, message_type, length


def decode_frame(data: bytes) -> DataFrame:
    """Decode exactly one complete DATA frame, retaining its header fields."""
    magic, message_type, length = decode_header(data[: DataFrame.header_size])
    if len(data) != DataFrame.header_size + length:
        raise ValueError("Invalid mx_eye frame size")
    (
        session,
        sequence,
        frame,
        acquisition_ns,
        tracking_start_ns,
        tracking_end_ns,
        send_ns,
        media_ns,
        x,
        y,
        pupil_x,
        pupil_y,
        cr_x,
        cr_y,
        pupil_area,
        template_ncc,
        flags,
    ) = DataFrame.TRACKING.unpack(data[DataFrame.header_size :])
    payload = TrackingPayload(
        session=session,
        sequence=sequence,
        frame=frame,
        acquisition_ns=acquisition_ns,
        tracking_start_ns=tracking_start_ns,
        tracking_end_ns=tracking_end_ns,
        send_ns=send_ns,
        media_ns=media_ns,
        x=x,
        y=y,
        pupil_x=pupil_x,
        pupil_y=pupil_y,
        cr_x=cr_x,
        cr_y=cr_y,
        pupil_area=pupil_area,
        template_ncc=template_ncc,
        flags=TrackingFlags(flags),
    )
    return DataFrame(
        magic=magic,
        message_type=message_type,
        length=length,
        payload=payload,
    )
