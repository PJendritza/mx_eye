"""CSV representation of tracking payloads, independent of the wire codec."""

from mx_eye_protocol.data_frame import DataFrame

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


def tracking_row(payload: DataFrame) -> tuple[int | float, ...]:
    return tuple(
        int(payload.flags) if attribute == "flags" else getattr(payload, attribute)
        for attribute in TRACKING_COLUMNS.values()
    )


"""Mux already-compressed camera JPEGs; no video decode or encoder in this path."""
import subprocess


class MjpegCopyWriter:
    def __init__(self, path, fps):
        self.log = path.with_suffix('.ffmpeg.log').open('wb')
        try:
            self.process = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                 '-f', 'mjpeg', '-framerate', str(fps), '-i', 'pipe:0',
                 '-map', '0:v:0', '-c:v', 'copy', '-an', '-y', str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log, bufsize=0)
        except Exception:
            self.log.close()
            raise

    def write(self, frame):
        view = memoryview(frame).cast('B')
        while view:
            size = self.process.stdin.write(view)
            if not size:
                raise IOError('FFmpeg stopped accepting video packets; inspect video.ffmpeg.log.')
            view = view[size:]

    def release(self):
        try:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass  # Still reap the failed process and report its exit code.
            try:
                code = self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise IOError('FFmpeg did not finalize the video in time.')
            if code:
                raise IOError(f'FFmpeg failed ({code}); inspect video.ffmpeg.log.')
        finally:
            self.log.close()
