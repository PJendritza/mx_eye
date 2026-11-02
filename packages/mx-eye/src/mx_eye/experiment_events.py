"""Durable experiment event journal, separate from the video writer process."""

import hashlib
import json
import time
from pathlib import Path

from mx_eye_protocol.events import ExperimentEvent

from .paths import data_root


class EventJournal:
    def __init__(self):
        self.latest = None
        self.path = ""
        self._seen = set()

    def contains(self, event):
        return event.event_id in self._seen

    def save(self, event: ExperimentEvent, recording_directory: str | None):
        # The run ID is user data, never a filesystem path.
        key = hashlib.sha256(event.run_id.encode()).hexdigest()[:24]
        path = data_root() / "events" / f"{key}.jsonl"
        row = {
            "received_ns": time.time_ns(),
            "recording_directory": recording_directory,
            "event": event.model_dump(mode="json"),
        }
        encoded = json.dumps(row, allow_nan=False) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
        if recording_directory:
            with (Path(recording_directory) / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(encoded)
        self._seen.add(event.event_id)
        self.latest, self.path = event, str(path)
