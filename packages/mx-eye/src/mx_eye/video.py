"""Timestamped video decoding and exact frame navigation with a bounded cache."""

from collections import OrderedDict, deque
from statistics import median
import math

import av


class VideoReader:
    def __init__(self, path, stop, limit=64 * 1024 * 1024):
        self.path, self.stop, self.limit = path, stop, limit
        self.total = None
        self.cache = OrderedDict()
        self.bytes = 0
        self._open()

    def _open(self):
        self.container = av.open(self.path)
        if not self.container.streams.video:
            self.container.close()
            raise ValueError("The selected file has no video stream.")
        self.stream = self.container.streams.video[0]
        self.stream.codec_context.thread_count = 1
        self.width, self.height = self.stream.width, self.stream.height
        rate = self.stream.average_rate
        self.fps = float(rate) if rate is not None else 0.0
        if not math.isfinite(self.fps) or self.fps <= 0:
            self.fps = 0.0
        self.estimated_total = self.stream.frames or 0
        if not self.estimated_total and self.container.duration and self.fps:
            self.estimated_total = round(self.container.duration / av.time_base * self.fps)
        tags = {k.lower(): v for k, v in self.container.metadata.items()}
        self.timing = ("Acquisition timestamps" if tags.get("mx_eye_timing") == "acquisition_monotonic"
                       else "Video timestamps")
        self.decoder = iter(self.container.decode(self.stream))
        self.next_index = 0
        self.origin_ns = self.previous_ns = None
        self.intervals = deque(maxlen=120)

    def read(self, target):
        target = max(0, int(target))
        if self.total is not None:
            target = min(target, max(0, self.total - 1))
        if target in self.cache:
            image, media = self.cache[target]
            return image, target, media
        if target < self.next_index:
            self.container.close()
            self._open()
        latest = None
        while not self.stop.is_set() and self.next_index <= target:
            frame = next(self.decoder, None)
            if frame is None:
                self.total = self.next_index
                if latest is not None:
                    return latest
                if self.total - 1 in self.cache:
                    image, media = self.cache[self.total - 1]
                    return image, self.total - 1, media
                return None
            index = self.next_index
            self.next_index += 1
            if frame.pts is not None and frame.time_base is not None:
                absolute_ns = round(frame.pts * frame.time_base * 1_000_000_000)
                if self.origin_ns is None:
                    self.origin_ns = absolute_ns
                media = absolute_ns - self.origin_ns
            elif self.fps:
                # File metadata only; never use the camera's requested FPS.
                media = (0 if self.previous_ns is None else self.previous_ns + round(1e9 / self.fps))
                self.timing = "Nominal FPS fallback (missing timestamps)"
            else:
                raise ValueError("Video has neither frame timestamps nor a valid frame rate.")
            if self.previous_ns is not None and media < self.previous_ns:
                raise ValueError("Video timestamps run backwards; accurate playback is unavailable.")
            if self.previous_ns is not None and media > self.previous_ns:
                self.intervals.append(media - self.previous_ns)
            self.previous_ns = media
            image = frame.to_ndarray(format="bgr24")
            latest = (image, index, media)
            if index in self.cache:
                self.bytes -= self.cache.pop(index)[0].nbytes
            self.cache[index] = (image, media)
            self.bytes += image.nbytes
            while self.bytes > self.limit and len(self.cache) > 1:
                _, (old, _) = self.cache.popitem(last=False)
                self.bytes -= old.nbytes
        return latest if not self.stop.is_set() else None

    @property
    def median_fps(self):
        return 1e9 / median(self.intervals) if self.intervals else None

    def close(self):
        self.container.close()


class PlaybackClock:
    """Schedule source timestamps; track achieved speed without dropping frames."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.anchor_media = self.anchor_wall = None
        self.sample_media = self.sample_wall = None

    def delay(self, media_ns, now, speed):
        media = media_ns / 1e9
        if self.anchor_wall is None:
            self.anchor_wall, self.anchor_media = now, media
        deadline = self.anchor_wall + (media - self.anchor_media) / speed
        # Rebase after lateness to avoid racing through frames to catch up.
        if now > deadline:
            self.anchor_wall += now - deadline
            deadline = now
        return max(0.0, deadline - now)

    def measured_speed(self, media_ns, now):
        media = media_ns / 1e9
        if self.sample_wall is None:
            self.sample_wall, self.sample_media = now, media
            return None
        elapsed = now - self.sample_wall
        if elapsed < 2.0:
            return None
        achieved = (media - self.sample_media) / elapsed
        self.sample_wall, self.sample_media = now, media
        return achieved
