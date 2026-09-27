"""Control-plane messages shared by the tracker service and the SDK.

Framing stays in the transport layer; these models only describe the JSON
payloads exchanged on the control and sync ports.
"""

from dataclasses import dataclass, field

PROTOCOL_VERSION = 1

CMD_STATUS = "status"
CMD_START = "start"
CMD_STOP = "stop"
CMD_SYNC = "sync"
CONTROL_COMMANDS = (CMD_STATUS, CMD_START, CMD_STOP)
SYNC_COMMANDS = (CMD_SYNC,)


@dataclass(frozen=True)
class StatusSnapshot:
    """Flat status payload sent on the control port and shown by the GUI."""

    state: str
    message: str = ""
    paused: bool = False
    session: int = None
    stats: dict = field(default_factory=dict)
    directory: str = ""
    network: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    priority: list = field(default_factory=list)
    server_errors: list = field(default_factory=list)
    protocol: int = PROTOCOL_VERSION
    ok: bool = True

    def to_dict(self):
        return dict(
            ok=self.ok,
            protocol=self.protocol,
            state=self.state,
            message=self.message,
            paused=self.paused,
            session=self.session,
            stats=dict(self.stats),
            directory=self.directory,
            network=dict(self.network),
            source=dict(self.source),
            priority=list(self.priority),
            server_errors=list(self.server_errors),
        )

    @classmethod
    def from_dict(cls, data):
        return cls(
            state=data.get("state", ""),
            message=data.get("message", ""),
            paused=bool(data.get("paused", False)),
            session=data.get("session"),
            stats=dict(data.get("stats") or {}),
            directory=data.get("directory", ""),
            network=dict(data.get("network") or {}),
            source=dict(data.get("source") or {}),
            priority=list(data.get("priority") or []),
            server_errors=list(data.get("server_errors") or []),
            protocol=data.get("protocol", PROTOCOL_VERSION),
            ok=bool(data.get("ok", True)),
        )


@dataclass(frozen=True)
class Request:
    """One request on the control (status/start/stop) or sync port."""

    command: str
    protocol: int = PROTOCOL_VERSION
    t1: int = None  # sync probes only

    def to_dict(self):
        out = dict(protocol=self.protocol, command=self.command)
        if self.t1 is not None:
            out["t1"] = self.t1
        return out

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Malformed control request")
        command = data.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("Missing control command")
        t1 = data.get("t1")
        if command == CMD_SYNC and t1 is None:
            raise ValueError("Sync request must carry t1")
        return cls(command=command, protocol=data.get("protocol"), t1=t1)


@dataclass(frozen=True)
class Reply:
    """One reply; exactly one of status, the sync timestamps, or error is set."""

    ok: bool = True
    error: str = ""
    t1: int = None
    t2: int = None
    t3: int = None
    status: StatusSnapshot = None

    def to_dict(self):
        if self.status is not None:
            return self.status.to_dict()
        out = dict(ok=self.ok)
        if not self.ok:
            out["error"] = self.error
            return out
        for key in ("t1", "t2", "t3"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Malformed control reply")
        if not data.get("ok"):
            return cls(ok=False, error=str(data.get("error", "")))
        if "state" in data:
            return cls(ok=True, status=StatusSnapshot.from_dict(data))
        return cls(ok=True, t1=data.get("t1"), t2=data.get("t2"), t3=data.get("t3"))
