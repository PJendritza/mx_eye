"""Session lifecycle and a single command request-response server."""

import base64
import concurrent.futures
import datetime
import json
import multiprocessing as mp
import queue
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from mx_eye_protocol.control import (
    Command,
    ControlReply,
    ControlRequest,
    NetworkStatus,
    SourceStatus,
    StatusSnapshot,
    TrackingStats,
)

from .config import MxEyeConfigStore, SourceMode
from .control_server import ControlServer
from .pipeline import (
    FrameRing,
    Mailbox,
    PreviewMailbox,
    capture_worker,
    tracking_worker,
    writer_worker,
)

STAT_NAMES = (
    "acquired",
    "tracked",
    "written",
    "enqueued",
    "tracking_skips",
    "mailbox_misses",
    "record_fault",
    "log_fault",
    "capture_fault",
    "tracking_fault",
    "send_errors",
    "source_fps",
    "source_index",
    "processing_us",
)


class Service:
    def __init__(self, config: MxEyeConfigStore):
        self.config = config
        self.ctx = mp.get_context("spawn")
        self._lock = threading.RLock()
        self._requests = queue.Queue()
        self._quit = threading.Event()
        self.state, self.message = "idle", "Ready"
        self.session = None
        self.run = None
        self.revision = 0
        self.last_stats = {}
        self._preview = None
        self._template_png = None
        self._template_image = None
        self.directory = ""
        self.source_info = {}
        self.camera_control_info = ""
        self.recording_path_info = ""
        self.priority_info = []
        self._server_errors = []
        self._server = None
        self._owner = threading.Thread(
            target=self._supervise, daemon=True, name="mx-eye supervisor"
        )
        self._owner.start()
        self._start_server()

    def _start_server(self):
        net = self.config.value.network
        server = ControlServer(
            (str(net.bind), net.control_port), self._dispatch_request
        )
        try:
            server.start()
        except Exception as exc:  # noqa: BLE001 - surface control worker startup failure
            self._server_errors.append(f"control server: {exc}")
            self.message = "; ".join(self._server_errors)
            return
        self._server = server

    def _stop_server(self):
        if self._server is not None:
            self._server.close()
            self._server = None

    def submit(self, command, **args):
        future = concurrent.futures.Future()
        self._requests.put((command, args, future))
        return future

    def snapshot(self):
        with self._lock:
            run = self.run
            stats = (
                {k: v.value for k, v in run["stats"].items()}
                if run
                else dict(self.last_stats)
            )
            return StatusSnapshot(
                state=self.state,
                message=self.message,
                paused=bool(run and run["paused"].is_set()),
                session=self.session,
                stats=TrackingStats.model_validate(
                    {k: v for k, v in stats.items() if k in TrackingStats.model_fields}
                ),
                directory=self.directory,
                network=NetworkStatus.model_validate(
                    self.config.value.network, from_attributes=True
                ),
                source=SourceStatus.model_validate(self.source_info),
                priority=list(self.priority_info),
                server_errors=list(self._server_errors),
            )

    def preview(self):
        """GUI reads a local latest-value cache; it never reads a worker pipe."""
        with self._lock:
            item, self._preview = self._preview, None
        return item

    def _collect_preview(self):
        with self._lock:
            run = self.run
        if not run:
            return
        item = run["preview"].get()
        if item:
            with self._lock:
                png = (
                    self.config.value.template.png
                    if self.config.value.template
                    else None
                )
                if png != self._template_png:
                    self._template_png = png
                    self._template_image = (
                        cv2.imdecode(
                            np.frombuffer(base64.b64decode(png), np.uint8),
                            cv2.IMREAD_GRAYSCALE,
                        )
                        if png
                        else None
                    )
                item["template"] = self._template_image
                self._preview = item
                if item["revision"] >= self.revision:
                    self.config.value.tracking = item["tracking"].model_copy(deep=True)
                    self.config.value.tracking.roi = tuple(item["result"]["roi"])
                    if self.config.value.template:
                        self.config.value.template.anchor = tuple(
                            item["result"]["template_anchor"]
                        )
                        self.config.value.template.center = tuple(
                            item["result"]["template_center"]
                        )

    def _dispatch_request(self, request: ControlRequest) -> ControlReply:
        if request.command is Command.STATUS:
            return ControlReply(status=self.snapshot())
        return ControlReply(status=self.submit(request.command).result(timeout=15))

    def _start(self):
        if self.run:
            if self.state == "running":
                return self.snapshot()  # idempotent START
            raise RuntimeError(
                "Wait until the previous recording has finished draining."
            )
        # Each worker process gets its own snapshot of the shared store.
        config = MxEyeConfigStore(self.config.value.model_copy(deep=True))
        if self._server_errors:
            raise RuntimeError("; ".join(self._server_errors))
        s = config.value.source
        if s.mode is SourceMode.VIDEO:
            cap = cv2.VideoCapture(s.path)
            try:
                if not cap.isOpened():
                    raise ValueError("Choose an existing, readable video file.")
                width, height = (
                    int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                )
            finally:
                cap.release()
        else:
            width, height = max(s.width, 1280), max(s.height, 800)
        if not 0 < width <= 16384 or not 0 < height <= 16384:
            raise ValueError("Unsupported source dimensions.")
        max_bytes = width * height * 3
        recordable = s.mode is not SourceMode.VIDEO
        c = self.ctx
        stats = {k: c.Value("d", 0) for k in STAT_NAMES}
        stats["record_acquired"] = c.Value("d", 0)
        run = dict(
            stats=stats,
            stop=c.Event(),
            capture_done=c.Event(),
            tracking_done=c.Event(),
            writer_done=c.Event(),
            paused=c.Event(),
            commands=c.Queue(64),
            capture_commands=c.Queue(16),
            preview=PreviewMailbox(c, max_bytes),
            events=c.Queue(64),
            tracked_frame=c.Value("q", 0),
            mailbox=Mailbox(c, max_bytes),
            samples=c.Queue(4096) if recordable else None,
            ring=FrameRing(
                c,
                max_bytes,
                max(2, int(config.value.recording.buffer_mb * 1024**2 / max_bytes)),
            )
            if recordable
            else None,
            recording=c.Event(),
            recording_generation=c.Value("q", 0),
            recording_stop=c.Event(),
            recording_capture_done=c.Value("q", 0),
            processes={},
            stop_time=None,
            ready=set(),
        )
        self.session = uuid.uuid4().int & ((1 << 64) - 1)
        self.revision = 0
        self.priority_info = []
        self.source_info = {}
        self.camera_control_info = ""
        self.recording_path_info = ""
        self._preview = None
        self.directory = ""
        self.state, self.message = "starting", "Opening source…"
        self.run = run
        try:
            run["writer_done"].set()
            tracker = c.Process(
                target=tracking_worker,
                name="mx-eye tracking",
                args=(
                    config,
                    self.session,
                    run["mailbox"],
                    run["commands"],
                    run["preview"],
                    run["samples"],
                    run["capture_done"],
                    run["tracking_done"],
                    run["stop"],
                    run["tracked_frame"],
                    stats,
                    run["events"],
                    run["recording"],
                    run["recording_stop"],
                    run["recording_generation"],
                ),
            )
            tracker.start()
            run["processes"]["tracker"] = tracker
            required = {"tracking_ready"}
            deadline = time.monotonic() + 10
            while not required <= run["ready"]:
                self._events()
                if stats["tracking_fault"].value or stats["record_fault"].value:
                    raise RuntimeError(self.message)
                if time.monotonic() > deadline:
                    raise TimeoutError("Workers did not initialize within 10 seconds.")
                time.sleep(0.01)
            capture = c.Process(
                target=capture_worker,
                name="mx-eye acquisition",
                args=(
                    config,
                    run["mailbox"],
                    run["ring"],
                    run["stop"],
                    run["capture_done"],
                    run["paused"],
                    run["capture_commands"],
                    run["tracked_frame"],
                    stats,
                    run["events"],
                    run["recording"],
                    run["recording_stop"],
                    run["recording_capture_done"],
                    run["recording_generation"],
                ),
            )
            capture.start()
            run["processes"]["capture"] = capture
            deadline = time.monotonic() + 10
            while "width" not in self.source_info:
                self._events()
                if stats["capture_fault"].value:
                    raise RuntimeError(self.message)
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "Source did not return its first frame within 10 seconds."
                    )
                time.sleep(0.01)
            self.state, self.message = "running", "Running"
            return self.snapshot()
        except Exception:
            self._stop()
            stats["capture_fault"].value = 1
            for name, event in [
                ("capture", "capture_done"),
                ("tracker", "tracking_done"),
                ("writer", "writer_done"),
            ]:
                if name not in run["processes"]:
                    run[event].set()
            raise

    def _record(self, enabled):
        run = self.run
        if not run or self.state != "running" or run["ring"] is None:
            raise RuntimeError("Start camera or simulation tracking before recording.")
        if not enabled:
            run["recording"].clear()
            run["recording_stop"].set()
            return self.snapshot()
        if run["recording"].is_set():
            return self.snapshot()
        previous = run["processes"].get("writer")
        if previous is not None:
            if not run["writer_done"].is_set() or previous.is_alive():
                raise RuntimeError(
                    "Wait for the previous recording to finish draining."
                )
            previous.join()
        if run["stats"]["record_fault"].value:
            raise RuntimeError("Restart tracking after resolving the recording fault.")
        for key in ("enqueued", "written", "record_acquired", "log_fault"):
            run["stats"][key].value = 0
        run["recording_stop"].clear()
        run["writer_done"].clear()
        run["ready"].discard("writer_ready")
        config = MxEyeConfigStore(self.config.value.model_copy(deep=True))
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.directory = str(
            Path(config.value.recording.directory).expanduser().resolve()
            / f"{stamp}_{self.session:016x}"
        )
        writer = self.ctx.Process(
            target=writer_worker,
            name="mx-eye recording",
            args=(
                config,
                self.session,
                self.directory,
                run["ring"],
                run["samples"],
                run["capture_done"],
                run["tracking_done"],
                run["writer_done"],
                run["stats"],
                run["events"],
            ),
            kwargs={
                "recording_capture_done": run["recording_capture_done"],
                "recording_generation": run["recording_generation"].value + 1,
            },
        )
        run["recording_generation"].value += 1
        try:
            writer.start()
            run["processes"]["writer"] = writer
            deadline = time.monotonic() + 10
            while "writer_ready" not in run["ready"]:
                self._events()
                if run["stats"]["record_fault"].value or writer.exitcode is not None:
                    raise RuntimeError(
                        self.message or "Recording could not initialize."
                    )
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "Recording did not initialize within 10 seconds."
                    )
                time.sleep(0.01)
        except Exception:
            run["recording_stop"].set()
            if writer.pid is None:
                run["writer_done"].set()
            raise
        run["recording"].set()
        return self.snapshot()

    def _stop(self):
        if self.run:
            self.run["recording"].clear()
            self.run["recording_stop"].set()
            self.run["stop"].set()
            if self.run["stop_time"] is None:
                self.run["stop_time"] = time.monotonic()
            self.state = "stopping"
        return self.snapshot()

    def _events(self):
        if not self.run:
            return
        try:
            while True:
                e = self.run["events"].get_nowait()
                kind = e["kind"]
                if kind in ("error", "record_error"):
                    self.message = e["message"]
                elif kind == "priority":
                    self.priority_info.append(e["message"])
                elif kind == "template":
                    self.config.value.template = e["template"]
                elif kind in ("source", "dimensions"):
                    self.source_info.update(
                        {k: v for k, v in e.items() if k in SourceStatus.model_fields}
                    )
                    if "camera_controls" in e:
                        self.camera_control_info = e["camera_controls"]
                    if "recording_path" in e:
                        self.recording_path_info = e["recording_path"]
                elif kind.endswith("_ready"):
                    self.run["ready"].add(kind)
        except queue.Empty:
            pass

    def _monitor(self):
        run = self.run
        if not run:
            return
        self._events()
        self._collect_preview()
        if run["stats"]["record_fault"].value and run["recording"].is_set():
            run["recording"].clear()
            run["recording_stop"].set()
        pairs = [
            ("capture", "capture_done", "capture_fault"),
            ("tracker", "tracking_done", "tracking_fault"),
            ("writer", "writer_done", "record_fault"),
        ]
        for name, done, fault in pairs:
            proc = run["processes"].get(name)
            if (
                proc is not None
                and proc.exitcode is not None
                and not run[done].is_set()
            ):
                run[done].set()
                run["stats"][fault].value = 3
                if name == "writer":
                    run["stats"]["log_fault"].value = 1
                else:
                    self._stop()
                self.message = f"{name} worker exited unexpectedly ({proc.exitcode})."
        if run["capture_done"].is_set() and self.state == "running":
            self._stop()
        if run["stop_time"] is not None:
            elapsed = time.monotonic() - run["stop_time"]
            for name, done, fault in pairs:
                proc = run["processes"].get(name)
                limit = 5 if name == "capture" else 30
                if proc is not None and proc.is_alive() and elapsed > limit:
                    proc.terminate()
                    proc.join(1)
                    run[done].set()
                    run["stats"][fault].value = 3
                    self.message = (
                        f"{name} did not stop; forced termination. Session incomplete."
                    )
        if all(
            run[k].is_set() for k in ("capture_done", "tracking_done", "writer_done")
        ):
            for proc in run["processes"].values():
                proc.join(0.05)
            if any(p.is_alive() for p in run["processes"].values()):
                return
            self._collect_preview()
            self.last_stats = {k: v.value for k, v in run["stats"].items()}
            faulty = any(
                self.last_stats[k]
                for k in (
                    "capture_fault",
                    "tracking_fault",
                    "record_fault",
                    "log_fault",
                )
            )
            self.state = "error" if faulty else "idle"
            if not faulty:
                self.message = (
                    "Session finished; recording finalized."
                    if self.directory
                    else "Session finished."
                )
            if self.directory and faulty:
                path = Path(self.directory) / "session.json"
                try:
                    data = json.loads(path.read_text()) if path.exists() else {}
                    data.update(
                        complete=False,
                        supervisor_message=self.message,
                        stats=self.last_stats,
                    )
                    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except (OSError, ValueError):
                    pass
            for k in ("commands", "capture_commands", "events", "samples"):
                if run[k] is not None:
                    run[k].cancel_join_thread()
                    run[k].close()
            self.run = None

    def _execute(self, command, args):
        if command == "record":
            return self._record(bool(args["enabled"]))
        if command == Command.START:
            return self._start()
        if command == Command.STOP:
            return self._stop()
        if command == "settings":
            if self.run:
                raise RuntimeError(
                    "Stop the session before changing source/network/recording settings."
                )
            updated = MxEyeConfigStore(args["config"].value.model_copy(deep=True))
            network_changed = updated.value.network != self.config.value.network
            self.config.replace(updated.value)
            if network_changed:
                self._stop_server()
                self._server_errors.clear()
                self._start_server()
                if self._server_errors:
                    raise RuntimeError("; ".join(self._server_errors))
            return self.snapshot()
        if command == "config":
            check = MxEyeConfigStore(self.config.value.model_copy(deep=True))
            check.value.tracking = args["tracking"].model_copy(deep=True)
            self.config.replace(check.value)
        if command == "speed":
            check = MxEyeConfigStore(self.config.value.model_copy(deep=True))
            check.value.source.speed = args["speed"]
            self.config.replace(check.value)
            if self.run and self.config.value.source.mode is SourceMode.VIDEO:
                self.run["capture_commands"].put_nowait(
                    dict(command="speed", speed=args["speed"])
                )
            return self.snapshot()
        if command == "load_template":
            self.config.value.template = args["template"]
        if command == "clear_template":
            self.config.value.template = None
        if command == "display":
            self.config.value.display.suspended = bool(args["suspended"])
            if "hz" in args:
                self.config.value.display.hz = args["hz"]
        if not self.run:
            if command == "roi":
                self.config.value.tracking.roi = tuple(args["roi"])
            return self.snapshot()
        if command == "pause":
            if self.config.value.source.mode is not SourceMode.VIDEO:
                raise ValueError("Only file playback can be paused.")
            if args["paused"]:
                self.run["paused"].set()
            else:
                self.run["paused"].clear()
        elif command in ("seek", "step"):
            if self.config.value.source.mode is not SourceMode.VIDEO:
                raise ValueError("Seek and step are only available for video files.")
            self.run["paused"].set()
            self.run["capture_commands"].put_nowait(dict(command=command, **args))
        else:
            self.revision += 1
            self.run["commands"].put_nowait(
                dict(command=command, revision=self.revision, **args)
            )
        return self.snapshot()

    def _supervise(self):
        while not self._quit.is_set():
            try:
                command, args, future = self._requests.get(timeout=0.01)
                try:
                    reply = self._execute(command, args)
                    future.set_result(reply)
                except Exception as exc:
                    self.message = str(exc)
                    future.set_exception(exc)
            except queue.Empty:
                pass
            self._monitor()

    def close(self):
        self.submit("stop").result(timeout=3)
        deadline = time.monotonic() + 33
        while self.run and time.monotonic() < deadline:
            time.sleep(0.02)
        self._stop_server()
        self._quit.set()
        self._owner.join(1)
