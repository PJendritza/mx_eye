"""CSV representation of tracking payloads, independent of the wire codec."""

from mx_eye_protocol.data_frame import TrackingPayload

# Output column name -> payload attribute; insertion order is the CSV order.
TRACKING_COLUMNS = {
    "session": "session",
    "sequence": "sequence",
    "source_frame": "frame",
    "acquisition_ns": "acquisition_ns",
    "tracking_start_ns": "tracking_start_ns",
    "tracking_end_ns": "tracking_end_ns",
    "send_ns": "send_ns",
    "media_ns": "media_ns",
    "x": "x",
    "y": "y",
    "pupil_x": "pupil_x",
    "pupil_y": "pupil_y",
    "cr_x": "cr_x",
    "cr_y": "cr_y",
    "pupil_area": "pupil_area",
    "template_ncc": "template_ncc",
    "flags": "flags",
}


def tracking_row(payload: TrackingPayload) -> tuple[int | float, ...]:
    return tuple(
        int(payload.flags) if attribute == "flags" else getattr(payload, attribute)
        for attribute in TRACKING_COLUMNS.values()
    )
