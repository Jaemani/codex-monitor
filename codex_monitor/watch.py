"""Change-only adapter. Waiting and sampling happen outside the model."""
import hashlib
import json
import os
from pathlib import Path
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ChangeWatcher:
    def __init__(self, path, source, event_type, emit, event_builder=None):
        self.path = Path(path)
        self.source, self.event_type, self.emit, self.event_builder = source, event_type, emit, event_builder

    def _event(self, previous, current):
        event = {"id": str(uuid.uuid4()), "source": self.source, "type": self.event_type,
                 "data": {"previous": previous, "current": current}}
        return self.event_builder(event) if self.event_builder else event

    def check(self, sample):
        state = json.loads(self.path.read_text()) if self.path.exists() else None
        if state is None:
            atomic_json(self.path, {"last": sample, "pending": None})
            return False
        if state["pending"]:
            self.emit(state["pending"])
            state["last"] = state["pending"]["data"]["current"]
            state["pending"] = None
            atomic_json(self.path, state)
        if sample == state["last"]:
            return False
        state["pending"] = self._event(state["last"], sample)
        atomic_json(self.path, state)
        self.emit(state["pending"])
        state.update(last=sample, pending=None)
        atomic_json(self.path, state)
        return True


def file_sample(path):
    path = Path(path)
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024*1024), b""):
                digest.update(block)
        return {"path": str(path.resolve()), "state": "present", "sha256": digest.hexdigest()}
    except FileNotFoundError:
        return {"path": str(path.resolve()), "state": "missing"}
    except OSError as exc:
        return {"path": str(path.resolve()), "state": "unreadable", "error": type(exc).__name__}
