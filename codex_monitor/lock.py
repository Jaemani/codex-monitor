"""OS-held process lock; SIGKILL releases ownership without stale PID guesses."""
from pathlib import Path
import os


class ProcessLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b"0"); self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise RuntimeError("another process owns this monitor state") from exc
        return self

    def __exit__(self, *_):
        if self.stream:
            self.stream.close()


def process_alive(path):
    try:
        with ProcessLock(path):
            return False
    except RuntimeError:
        return True
