"""Operator-only configuration and a process-wide/cross-process account lock."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from threading import RLock

from dotenv import dotenv_values

from .client import MenzaClient

_lock = RLock()


def settings() -> tuple[str, str, Path]:
    # The privileged launcher supplies an absolute, root-controlled path. MCP
    # requests cannot choose files or change this environment.
    env_file = Path(os.environ.get("MENZA_ENV_FILE", ".env")).expanduser()
    values = dotenv_values(env_file) if env_file.is_file() else {}
    username = os.environ.get("MENZA_USERNAME") or values.get("MENZA_USERNAME") or values.get("username_email")
    password = os.environ.get("MENZA_PASSWORD") or values.get("MENZA_PASSWORD") or values.get("password")
    if not username or not password:
        raise ValueError("Configure MENZA_USERNAME/MENZA_PASSWORD in the operator credential file")
    state = Path(os.environ.get("MENZA_STATE_DIR", "~/.local/state/menza-hermes")).expanduser()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    return username, password, state


@contextmanager
def account():
    """Serialize all account operations, including watcher and separate MCP processes.

    Re-open cookies under the lock so a process cannot overwrite a newer login.
    """
    with _lock:
        username, password, state = settings()
        fd = os.open(state / "account.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            client = MenzaClient(username, password, state_dir=state)
            try:
                yield client
            finally:
                client.session.close()
        finally:
            os.close(fd)
