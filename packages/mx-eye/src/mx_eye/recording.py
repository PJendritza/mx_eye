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


class TimestampedVideoWriter:
    """One MKV frame per acquired image; monotonic acquisition time is its PTS."""

    def __init__(self, path, nominal_fps):
        import av
        from fractions import Fraction

        self.av = av
        self.time_base = Fraction(1, 1_000_000)
        self.container = av.open(str(path), "w", format="matroska")
        self.container.metadata["MX_EYE_TIMING"] = "acquisition_monotonic"
        self.rate = Fraction(str(nominal_fps or 30)).limit_denominator(100000)
        self.stream = None
        self.origin = self.last_ns = None
        self.pending = None
        self.last_duration = max(1, round(1 / self.rate / self.time_base))
        self.codec = None
        self.shape = None

    def write(self, image, acquired_monotonic_ns):
        import io

        if self.last_ns is not None and acquired_monotonic_ns <= self.last_ns:
            raise ValueError("Acquisition timestamps must increase strictly.")
        if self.stream is None:
            self.origin = acquired_monotonic_ns
            if image.ndim == 1:
                # Probe only the first JPEG; subsequent packets are copied unchanged.
                with self.av.open(io.BytesIO(image.tobytes()), format="mjpeg") as source:
                    self.stream = self.container.add_stream_from_template(source.streams.video[0])
                self.codec = "mjpeg_copy"
            else:
                self.stream = self.container.add_stream("ffv1", rate=self.rate)
                self.stream.width, self.stream.height = image.shape[1], image.shape[0]
                self.stream.pix_fmt = "bgr0"
                self.stream.codec_context.thread_count = 1
                self.stream.codec_context.time_base = self.time_base
                self.codec = "ffv1"
                self.shape = image.shape
            self.stream.time_base = self.time_base
        pts = (acquired_monotonic_ns - self.origin + 500) // 1000
        self.last_ns = acquired_monotonic_ns
        if self.codec == "mjpeg_copy":
            if image.ndim != 1:
                raise ValueError("Camera changed from compressed to decoded frames.")
            packet = self.av.Packet(image.tobytes())
            packet.stream = self.stream
            packet.pts = packet.dts = pts
            packet.time_base = self.time_base
            packet.is_keyframe = True
            self._mux(packet)
        else:
            if image.shape != self.shape:
                raise ValueError("Source dimensions changed during recording.")
            frame = self.av.VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts, frame.time_base = pts, self.time_base
            for packet in self.stream.encode(frame):
                self._mux(packet)

    def _mux(self, packet):
        if self.pending is not None:
            duration = round((packet.pts * packet.time_base
                              - self.pending.pts * self.pending.time_base)
                             / self.pending.time_base)
            if duration <= 0:
                raise ValueError("Video timestamps are not strictly increasing.")
            self.pending.duration = duration
            self.last_duration = duration
            self.container.mux(self.pending)
        self.pending = packet

    def release(self):
        try:
            if self.codec == "ffv1":
                for packet in self.stream.encode(None):
                    self._mux(packet)
            if self.pending is not None:
                # The final frame has no successor: use the last observed interval.
                self.pending.duration = self.last_duration
                self.container.mux(self.pending)
                self.pending = None
        finally:
            self.container.close()
