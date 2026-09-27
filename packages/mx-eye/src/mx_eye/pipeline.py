"""Acquisition, tracking and recording workers. Each runs in its own process."""

import base64
import csv
import json
import math
import os
import queue
import signal
import socket
import time
from pathlib import Path

import cv2
import numpy as np
import psutil
from mx_eye_protocol.data_frame import (
    DataFrame,
    TrackingFlags,
    TrackingPayload,
)

from .config import (
    CameraBackend,
    PupilCoordinates,
    RecordingCodec,
    SourceMode,
    TrackingMode,
    Transport,
)
from .recording import TRACKING_COLUMNS, tracking_row
from .tracking import Tracker
from .transport import Publisher
from .video import VideoReader


class Mailbox:
    """One consistent latest frame; neither reader nor writer waits for its lock."""

    def __init__(self, ctx, max_bytes):
        self.pixels = ctx.RawArray("B", max_bytes)
        self.meta = ctx.RawArray(
            "q", 7
        )  # frame, acquisition, media, source index, navigation ID, h, w
        self.lock = ctx.Lock()

    def put(self, frame, meta):
        if not self.lock.acquire(False):
            return False
        try:
            np.frombuffer(self.pixels, np.uint8)[: frame.size] = frame.reshape(-1)
            self.meta[:] = [*meta, *frame.shape[:2]]
            return True
        finally:
            self.lock.release()

    def get(self, after=0):
        if not self.lock.acquire(False):
            return None
        try:
            fid, acquired, media, source_index, navigation_id, h, w = self.meta[:]
            if fid <= after:
                return None
            frame = (
                np.frombuffer(self.pixels, np.uint8)[: h * w * 3]
                .reshape(h, w, 3)
                .copy()
            )
            return frame, (fid, acquired, media, source_index, navigation_id)
        finally:
            self.lock.release()


class FrameRing:
    """Single-producer/single-consumer bounded FIFO, with explicit slot ownership.

    Disk I/O happens only after get() has copied and released its shared slot.
    Semaphores synchronize frame bytes/metadata without reading torn frames.
    """

    def __init__(self, ctx, max_bytes, capacity):
        self.max_bytes, self.capacity = max_bytes, capacity
        self.pixels = ctx.RawArray("B", max_bytes * capacity)
        self.meta = ctx.RawArray("q", 5 * capacity)
        self.free = ctx.Semaphore(capacity)
        self.ready = ctx.Semaphore(0)
        self.write_index = self.read_index = 0

    def put(self, frame, meta):
        if not self.free.acquire(False):
            return False
        i = self.write_index % self.capacity
        np.frombuffer(
            self.pixels, np.uint8, count=frame.size, offset=i * self.max_bytes
        )[:] = frame.reshape(-1)
        self.meta[i * 5 : i * 5 + 5] = [*meta, *frame.shape[:2]]
        self.write_index += 1
        self.ready.release()
        return True

    def get(self, timeout=0.02):
        if not self.ready.acquire(timeout=timeout):
            return None
        i = self.read_index % self.capacity
        fid, acquired, media, h, w = self.meta[i * 5 : i * 5 + 5]
        frame = (
            np.frombuffer(
                self.pixels, np.uint8, count=h * w * 3, offset=i * self.max_bytes
            )
            .reshape(h, w, 3)
            .copy()
        )
        self.read_index += 1
        self.free.release()
        return frame, (fid, acquired, media)


class PreviewMailbox:
    """Latest frame plus metadata in shared memory; no image pickles or pipes.

    Reads and writes both use try-locks. A stalled/dead UI reader or worker never
    blocks the other side, including during forced process shutdown.
    """

    def __init__(self, ctx, max_bytes):
        self.pixels = ctx.RawArray("B", max_bytes)
        self.metadata = ctx.RawArray("B", 32768)
        self.header = ctx.RawArray("q", 4)  # revision, JSON bytes, height, width
        self.lock = ctx.Lock()
        self.last_read = 0

    def put(self, frame, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(raw) > len(self.metadata) or not self.lock.acquire(False):
            return False
        try:
            np.frombuffer(self.pixels, np.uint8)[: frame.size] = frame.reshape(-1)
            np.frombuffer(self.metadata, np.uint8)[: len(raw)] = np.frombuffer(
                raw, np.uint8
            )
            self.header[:] = [self.header[0] + 1, len(raw), *frame.shape[:2]]
        finally:
            self.lock.release()
        return True

    def get(self):
        if not self.lock.acquire(False):
            return None
        try:
            revision, length, h, w = self.header[:]
            if revision <= self.last_read:
                return None
            raw = bytes(self.metadata[:length])
            frame = (
                np.frombuffer(self.pixels, np.uint8)[: h * w * 3]
                .reshape(h, w, 3)
                .copy()
            )
            self.last_read = revision
        finally:
            self.lock.release()
        payload = json.loads(raw)
        x, y, rw, rh = map(int, payload["result"]["roi"])
        payload.update(frame=frame, crop=frame[y : y + rh, x : x + rw], scale=1)
        return payload


def priority(role):
    """Best effort only. Never request real-time scheduling or administrator rights."""
    try:
        process = psutil.Process()
        if os.name == "nt":
            process.nice(
                psutil.ABOVE_NORMAL_PRIORITY_CLASS
                if role in ("capture", "tracking")
                else psutil.BELOW_NORMAL_PRIORITY_CLASS
            )
        else:
            process.nice(-5 if role in ("capture", "tracking") else 5)
        return f"{role}: priority set"
    except (psutil.Error, PermissionError, OSError):
        return f"{role}: normal priority (OS denied adjustment)"


def report(events, kind, **data):
    try:
        events.put_nowait(dict(kind=kind, **data))
    except queue.Full:
        pass


def synthetic_frame(index, width, height, fps):
    """Deterministic artificial eye for installation and transport checks."""
    t = index / fps
    frame = np.full((height, width, 3), 155, np.uint8)
    cx, cy = (
        int(width * 0.25 + 22 * math.sin(t * 1.6)),
        int(height * 0.25 + 12 * math.cos(t * 1.1)),
    )
    cv2.ellipse(frame, (cx, cy), (44, 25), 0, 0, 360, (95, 95, 95), -1)
    if t % 7 < 6.8:
        pupil_center = (cx + int(3 * math.sin(t * 5)), cy + int(2 * math.sin(t * 2.9)))
        cv2.ellipse(frame, pupil_center, (11, 9), 10, 0, 360, (15, 15, 15), -1)
        cv2.circle(frame, (cx + 5, cy - 4), 2, (245, 245, 245), -1)
    return frame


def capture_worker(
    config, mailbox, ring, stop, done, paused, commands, tracked_frame, stats, events
):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    cv2.setNumThreads(1)
    report(events, "priority", message=priority("capture"))
    source = config.value.source
    cap = None
    reader = None
    navigation_id = 0
    fid = index = 0
    due = time.perf_counter()
    first_size = None
    try:
        mode = source.mode
        if mode is not SourceMode.SIMULATION:
            backend = {
                CameraBackend.AUTO: cv2.CAP_ANY,
                CameraBackend.DSHOW: cv2.CAP_DSHOW,
                CameraBackend.MSMF: cv2.CAP_MSMF,
                CameraBackend.V4L2: cv2.CAP_V4L2,
            }[source.backend]
            if mode is SourceMode.VIDEO:
                reader = VideoReader(source.path, stop)
                cap = reader.cap
            else:
                if backend == cv2.CAP_ANY and os.name == "nt":
                    backend = cv2.CAP_DSHOW
                cap = cv2.VideoCapture(int(source.camera), backend)
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*source.fourcc))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, source.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, source.height)
                cap.set(cv2.CAP_PROP_FPS, source.fps)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                raise RuntimeError(
                    "Cannot open source. Check camera index/backend or video path."
                )
            reported_fps = cap.get(cv2.CAP_PROP_FPS)
            fps = (
                reported_fps
                if math.isfinite(reported_fps) and 1 <= reported_fps <= 1000
                else source.fps
            )
            total = (
                int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                if mode is SourceMode.VIDEO
                else 0
            )
        else:
            fps, total = source.fps, 0
        stats["source_fps"].value = fps
        report(events, "source", fps=fps, total=total, mode=mode)
        if mode is SourceMode.CAMERA:
            code = int(cap.get(cv2.CAP_PROP_FOURCC))
            actual_format = (
                "".join(chr((code >> shift) & 255) for shift in (0, 8, 16, 24))
                if code
                else "unknown"
            )
            if not actual_format.isprintable():
                actual_format = "unknown"
            report(
                events,
                "source",
                actual_format=actual_format,
                driver_fps=reported_fps
                if math.isfinite(reported_fps) and 1 <= reported_fps <= 1000
                else None,
                requested_format=source.fourcc,
                requested_fps=source.fps,
            )
        while not stop.is_set():
            step = False
            try:
                while True:
                    cmd = commands.get_nowait()
                    if mode is SourceMode.VIDEO and cmd["command"] == "speed":
                        source.speed = cmd["speed"]
                        due = time.perf_counter()
                    if mode is SourceMode.VIDEO and cmd["command"] == "seek":
                        index = max(0, int(cmd["frame"]))
                        navigation_id = cmd.get("navigation_id", navigation_id)
                        paused.set()
                        step = True
                    if mode is SourceMode.VIDEO and cmd["command"] == "step":
                        index = max(
                            0,
                            int(cmd.get("from_frame", stats["source_index"].value))
                            + int(cmd.get("direction", 1)),
                        )
                        navigation_id = cmd.get("navigation_id", navigation_id)
                        paused.set()
                        step = True
                    if step:
                        due = time.perf_counter()
                        break  # Process each frame-step command on its own frame.
            except queue.Empty:
                pass
            if (
                mode is SourceMode.VIDEO
                and reader.total is not None
                and index >= reader.total
                and not step
            ):
                paused.set()  # Keep the last frame available for backward stepping.
            if mode is SourceMode.VIDEO and paused.is_set() and not step and fid:
                stop.wait(0.005)
                due = time.perf_counter()
                continue
            if mode in (SourceMode.VIDEO, SourceMode.SIMULATION):
                delay = due - time.perf_counter()
                if delay > 0 and stop.wait(delay):
                    break
            if mode is SourceMode.SIMULATION:
                frame = synthetic_frame(index, source.width, source.height, fps)
                ok = True
            elif mode is SourceMode.VIDEO:
                decoded = reader.read(index)
                ok = decoded is not None
                if ok:
                    frame, index, msec = decoded
                if reader.total is not None:
                    total = reader.total
                    report(events, "source", total=total, total_exact=True)
                    if index >= total - 1:
                        paused.set()
            else:
                ok, frame = cap.read()
            acquired = (
                time.perf_counter_ns()
            )  # Host read-return time, NOT sensor exposure.
            if not ok:
                if mode is SourceMode.CAMERA:
                    raise RuntimeError("Camera read failed or camera disconnected.")
                report(events, "eof")
                break
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            frame = np.ascontiguousarray(frame)
            if frame.size > len(mailbox.pixels):
                raise RuntimeError(
                    "Actual frame exceeds the configured allocation. Increase source dimensions."
                )
            if first_size is None:
                first_size = frame.shape
                report(
                    events, "dimensions", width=frame.shape[1], height=frame.shape[0]
                )
            elif frame.shape != first_size:
                raise RuntimeError("Source dimensions changed during the session.")
            media = -1
            if mode is SourceMode.VIDEO:
                media = (
                    int(msec * 1e6)
                    if math.isfinite(msec) and (msec > 0 or index == 0)
                    else int(index / fps * 1e9)
                )
            elif mode is SourceMode.SIMULATION:
                media = int(index / fps * 1e9)
            fid += 1
            stats["acquired"].value = fid
            stats["source_index"].value = index
            # Publish to tracking BEFORE touching the recording ring.
            if not mailbox.put(frame, (fid, acquired, media, index, navigation_id)):
                stats["mailbox_misses"].value += 1
                # File playback waits for each frame; never silently skip it.
                if mode is SourceMode.VIDEO:
                    while not stop.is_set() and not mailbox.put(
                        frame, (fid, acquired, media, index, navigation_id)
                    ):
                        stop.wait(0.0005)
            if ring is not None and not stats["record_fault"].value:
                if ring.put(frame, (fid, acquired, media)):
                    stats["enqueued"].value += 1
                else:
                    stats["record_fault"].value = 1
                    report(
                        events,
                        "record_error",
                        message="Recording buffer overflow. Recording stopped; tracking continues. Session is incomplete.",
                    )
            index += 1
            if mode is SourceMode.VIDEO:
                while not stop.is_set() and tracked_frame.value < fid:
                    stop.wait(0.0005)
            if mode in (SourceMode.VIDEO, SourceMode.SIMULATION):
                due = max(due + 1 / (fps * source.speed), time.perf_counter())
    except Exception as exc:
        stats["capture_fault"].value = 1
        report(events, "error", message=str(exc))
    finally:
        if reader is not None:
            reader.close()
        if cap is not None:
            cap.release()
        done.set()


def tracking_worker(
    config,
    session,
    mailbox,
    commands,
    preview,
    samples,
    capture_done,
    done,
    stop,
    tracked_frame,
    stats,
    events,
):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    cv2.setNumThreads(1)
    report(events, "priority", message=priority("tracking"))
    core = Tracker(config.value.tracking)
    if config.value.template:
        restore_template(core, config.value.template)
    net = config.value.network
    pub, udp = None, None
    frame_id = seq = 0
    revision = 0
    last_preview = 0
    current = None
    last_media = -1
    try:
        if net.transport is Transport.TCP:
            pub = Publisher(net.bind, net.data_port)
        else:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setblocking(False)
            destination = (str(net.udp_host), net.data_port)
        report(events, "tracking_ready")
        while True:
            changed = False
            try:
                for _ in range(32):
                    cmd = commands.get_nowait()
                    revision = cmd.get("revision", revision)
                    kind = cmd["command"]
                    if kind == "config":
                        previous_mode = core.config.tracking_mode
                        core.config = cmd["tracking"].model_copy(deep=True)
                        if previous_mode != core.config.tracking_mode:
                            core.clear_feature_history()
                    elif kind == "roi":
                        core.roi = list(cmd["roi"])
                        core.clamp_roi()
                        core.clear_feature_history()
                    elif kind == "reset":
                        core.clear_feature_history()
                    elif kind == "search":
                        core.template_anchor = tuple(cmd["center"])
                        core.config.template_search_size = cmd["size"]
                    elif kind == "load_template":
                        restore_template(core, cmd["template"])
                    elif kind == "clear_template":
                        core.clear_template()
                    elif core.current_frame is not None and kind == "pick":
                        core.pick_feature(cmd["kind"], *cmd["point"])
                    elif core.current_frame is not None and kind == "template":
                        core.set_template(*cmd["point"])
                    if kind in ("template", "clear_template"):
                        report(events, "template", template=serialize_template(core))
                    changed = True
            except queue.Empty:
                pass
            fresh = mailbox.get(frame_id)
            finishing = fresh is None and capture_done.is_set()
            if fresh is None:
                if finishing and current is None:
                    break
                if finishing:
                    changed = True
                elif not changed or current is None:
                    time.sleep(0.0005)
                    continue
            if fresh is not None:
                current, meta = fresh
                fid, acquired, media, source_index, navigation_id = meta
                if (
                    media >= 0
                    and last_media >= 0
                    and (media <= last_media or media - last_media > 2e9)
                ):
                    core.clear_feature_history()
                last_media = media
                delta = max(1, fid - frame_id)
                stats["tracking_skips"].value += max(0, delta - 1)
                frame_id = fid
            start = time.perf_counter_ns()
            result = core.process(
                current,
                frame_id,
                advance=fresh is not None,
                frame_delta=delta if fresh is not None else 1,
            )
            end = time.perf_counter_ns()
            if fresh is not None:
                seq += 1
                pupil, cr = result["pupil"], result["cr"]
                nan = float("nan")
                flags = TrackingFlags.NONE
                if result["valid"]:
                    flags |= TrackingFlags.VALID
                if pupil:
                    flags |= TrackingFlags.PUPIL
                if cr:
                    flags |= TrackingFlags.CR
                if core.config.tracking_mode is TrackingMode.PUPIL_ONLY:
                    flags |= TrackingFlags.PUPIL_ONLY
                    if core.config.pupil_coordinates is PupilCoordinates.RELATIVE:
                        flags |= TrackingFlags.ROI_RELATIVE
                if config.value.source.mode is SourceMode.SIMULATION:
                    flags |= TrackingFlags.SIMULATION
                send = time.perf_counter_ns()
                payload = TrackingPayload(
                    session=session,
                    sequence=seq,
                    frame=frame_id,
                    acquisition_ns=acquired,
                    tracking_start_ns=start,
                    tracking_end_ns=end,
                    send_ns=send,
                    media_ns=media,
                    x=result["x"],
                    y=result["y"],
                    pupil_x=pupil["x"] if pupil else nan,
                    pupil_y=pupil["y"] if pupil else nan,
                    cr_x=cr["x"] if cr else nan,
                    cr_y=cr["y"] if cr else nan,
                    pupil_area=pupil["area"] if pupil else nan,
                    template_ncc=result["template_score"],
                    flags=flags,
                )
                data = DataFrame.from_payload(payload).encode()
                try:
                    if pub is not None:
                        stats["send_errors"].value += pub.send(data)
                    else:
                        udp.sendto(data, destination)
                except (BlockingIOError, OSError):
                    stats["send_errors"].value += 1
                if samples is not None:
                    try:
                        samples.put_nowait(payload)
                    except queue.Full:
                        if not stats["log_fault"].value:
                            report(
                                events,
                                "record_error",
                                message="Tracking log buffer overflow; log is incomplete. Tracking and video continue.",
                            )
                        stats["log_fault"].value = 1
                stats["tracked"].value = seq
                stats["processing_us"].value = (end - start) / 1000
            now = time.perf_counter()
            if (
                changed
                or (fresh is not None and config.value.source.mode is SourceMode.VIDEO)
                or now - last_preview >= 1 / config.value.display.hz
            ):
                payload = dict(
                    result=result,
                    tracking=core.config.model_copy(deep=True),
                    frame_id=frame_id,
                    media_ns=media,
                    processing_ms=(end - start) / 1e6,
                    revision=revision,
                )
                payload.update(source_index=source_index, navigation_id=navigation_id)
                published = preview.put(current, payload)
                if config.value.source.mode is SourceMode.VIDEO:
                    while not published and not stop.is_set():
                        stop.wait(0.001)
                        published = preview.put(current, payload)
                if published:
                    last_preview = now
            if fresh is not None:
                tracked_frame.value = frame_id
            if finishing:
                break
    except Exception as exc:
        stats["tracking_fault"].value = 1
        report(events, "error", message=f"Tracking: {exc}")
        stop.set()
    finally:
        if pub is not None:
            pub.close()
        if udp is not None:
            udp.close()
        # Ensure the feeder flushes sample rows before declaring tracking done.
        if samples is not None:
            if stats["record_fault"].value == 3:
                samples.cancel_join_thread()
            samples.close()
            if stats["record_fault"].value != 3:
                samples.join_thread()
        done.set()


def serialize_template(core):
    if core.template is None:
        return None
    ok, data = cv2.imencode(".png", core.template)
    return (
        dict(
            png=base64.b64encode(data).decode("ascii"),
            anchor=core.template_anchor,
            center=core.template_last_center,
        )
        if ok
        else None
    )


def restore_template(core, data):
    image = cv2.imdecode(
        np.frombuffer(base64.b64decode(data.png), np.uint8), cv2.IMREAD_GRAYSCALE
    )
    if image is None:
        raise ValueError("Invalid saved template")
    core.template = image
    core.template_anchor = data.anchor
    core.template_last_center = data.center


def writer_worker(
    config,
    session,
    directory,
    ring,
    samples,
    capture_done,
    tracking_done,
    done,
    stats,
    events,
    test_delay=0,
):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    cv2.setNumThreads(1)
    report(events, "priority", message=priority("recording"))
    folder = Path(directory)
    writer = None
    error = ""
    video_index = 0
    shape = None
    frame_file = sample_file = None
    try:
        folder.mkdir(parents=True, exist_ok=False)
        config.save(folder / "config.json")
        frame_file = (folder / "frames.csv").open("w", newline="", encoding="utf-8")
        sample_file = (folder / "tracking.csv").open("w", newline="", encoding="utf-8")
        fw, sw = csv.writer(frame_file), csv.writer(sample_file)
        fw.writerow(["video_index", "source_frame", "acquisition_ns", "media_ns"])
        sw.writerow(TRACKING_COLUMNS)
        report(events, "writer_ready")
        while True:
            for _ in range(256):
                try:
                    sw.writerow(tracking_row(samples.get_nowait()))
                except queue.Empty:
                    break
            item = ring.get(timeout=0.01)
            if item is not None:
                frame, (fid, acquired, media) = item
                if writer is None:
                    shape = frame.shape[:2]
                    fps = stats["source_fps"].value or config.value.source.fps
                    filename = (
                        "video.avi"
                        if config.value.recording.codec is RecordingCodec.MJPG
                        else "video.mkv"
                    )
                    writer = cv2.VideoWriter(
                        str(folder / filename),
                        cv2.VideoWriter_fourcc(*config.value.recording.codec),
                        fps,
                        (shape[1], shape[0]),
                    )
                    if not writer.isOpened():
                        raise RuntimeError(
                            "Video encoder could not open. Try MJPG or another recording directory."
                        )
                if test_delay:
                    time.sleep(test_delay)
                writer.write(frame)
                fw.writerow([video_index, fid, acquired, media])
                video_index += 1
                stats["written"].value = video_index
                if video_index % 60 == 0:
                    frame_file.flush()
                    sample_file.flush()
            elif (
                capture_done.is_set()
                and tracking_done.is_set()
                and stats["written"].value >= stats["enqueued"].value
            ):
                # tracking_done is set only after its sample-queue feeder flushed.
                while True:
                    try:
                        sw.writerow(samples.get_nowait())
                    except queue.Empty:
                        break
                break
    except Exception as exc:
        error = str(exc)
        stats["record_fault"].value = 2
        report(
            events,
            "record_error",
            message=f"Recording failed: {error}. Tracking continues.",
        )
        # Keep consuming log rows so a writer failure cannot deadlock tracker exit.
        while not tracking_done.is_set():
            try:
                samples.get(timeout=0.05)
            except queue.Empty:
                pass
    finally:
        if writer is not None:
            writer.release()
            # OpenCV write() has no per-frame success return. Verify the finalized
            # container is readable and reports the expected frame count.
            check = cv2.VideoCapture(str(folder / filename))
            count = int(check.get(cv2.CAP_PROP_FRAME_COUNT)) if check.isOpened() else -1
            readable, _ = check.read()
            check.release()
            if not readable or count != video_index:
                error = f"Video finalization check failed: expected {video_index} frames, container reports {count}."
                stats["record_fault"].value = 2
                report(events, "record_error", message=error)
        for file in (frame_file, sample_file):
            if file is not None:
                file.close()
        summary = dict(
            session=session,
            acquired=int(stats["acquired"].value),
            written=video_index,
            tracking_samples=int(stats["tracked"].value),
            complete=(
                not error
                and not stats["record_fault"].value
                and not stats["capture_fault"].value
                and video_index == stats["acquired"].value
            ),
            tracking_log_complete=not bool(stats["log_fault"].value),
            recording_fault=int(stats["record_fault"].value),
            error=error,
            timestamp_basis="host monotonic clock immediately after read(); not exposure time",
            video_timing="constant nominal FPS; use frames.csv for measured frame times",
            camera_driver_drops="not observable through this UVC/OpenCV backend",
        )
        try:
            (folder / "session.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            stats["record_fault"].value = 2
            report(
                events,
                "record_error",
                message=f"Could not finalize session metadata: {exc}",
            )
        done.set()
