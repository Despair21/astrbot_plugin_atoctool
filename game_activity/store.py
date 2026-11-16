"""Atomic, crash-tolerant persistence for snapshots and plugin state."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

_GAME_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_STATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class Store:
    """Persist per-game snapshots and small state files under the data dir."""

    def __init__(self, data_root: Path, logger) -> None:
        self.data_root = Path(data_root)
        self.logger = logger
        self.snapshots_dir = self.data_root / "snapshots"
        self.state_dir = self.data_root / "state"
        self._write_lock = threading.Lock()

    def ensure_dirs(self) -> None:
        """Create the data directory tree with restrictive permissions."""
        for directory in (self.data_root, self.snapshots_dir, self.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass

    def probe_writable(self) -> bool:
        """Return True when a probe file can be written and removed."""
        try:
            self.ensure_dirs()
            fd, tmp = tempfile.mkstemp(dir=str(self.data_root), suffix=".probe")
            os.close(fd)
            os.unlink(tmp)
            return True
        except OSError as exc:
            self.logger.warning("Plugin data directory is not writable: %s", exc)
            return False

    def cleanup_temp_files(self, max_age_seconds: float = 3600) -> int:
        """Remove stale temporary files left behind by interrupted writes."""
        removed = 0
        cutoff = time.time() - max_age_seconds
        for directory in (self.snapshots_dir, self.state_dir):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.tmp"):
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    self.logger.warning("Failed to remove temp file %s: %s", path, exc)
        return removed

    def load_snapshot(self, game_id: str) -> dict:
        """Load one game snapshot, returning ``{}`` when absent or unreadable."""
        self._validate_game_id(game_id)
        return self._read_json(self.snapshots_dir / f"{game_id}.json", {})

    def save_snapshot(self, game_id: str, data: dict) -> None:
        """Persist one game snapshot atomically."""
        self._validate_game_id(game_id)
        self.ensure_dirs()
        self._atomic_write(self.snapshots_dir / f"{game_id}.json", data)

    def load_state(self, name: str, default: dict | None = None) -> dict:
        """Load a state document such as ``reminders`` or ``http_cache``."""
        self._validate_state_name(name)
        fallback = copy.deepcopy(default) if default else {}
        return self._read_json(self.state_dir / f"{name}.json", fallback)

    def save_state(self, name: str, data: dict) -> None:
        """Persist a state document atomically."""
        self._validate_state_name(name)
        self.ensure_dirs()
        self._atomic_write(self.state_dir / f"{name}.json", data)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _validate_game_id(game_id: str) -> None:
        if not isinstance(game_id, str) or not _GAME_ID_RE.match(game_id):
            raise ValueError(f"invalid game_id: {game_id!r}")

    @staticmethod
    def _validate_state_name(name: str) -> None:
        if not isinstance(name, str) or not _STATE_NAME_RE.match(name):
            raise ValueError(f"invalid state name: {name!r}")

    def _read_json(self, path: Path, default: dict) -> dict:
        if not path.exists():
            return dict(default)
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.logger.warning("Corrupt JSON in %s: %s", path.name, exc)
            self._quarantine(path)
            return dict(default)
        if not isinstance(data, dict):
            self.logger.warning("Unexpected JSON root type in %s", path.name)
            self._quarantine(path)
            return dict(default)
        return data

    def _quarantine(self, path: Path) -> None:
        target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, target)
            self.logger.warning("Moved unreadable file to %s", target.name)
        except OSError as exc:
            self.logger.warning("Failed to quarantine %s: %s", path.name, exc)

    def _atomic_write(self, path: Path, data: dict) -> None:
        # Serialize writers: concurrent os.replace calls on Windows can raise
        # PermissionError while another thread holds the destination handle.
        with self._write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(
                        data,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name == "posix":
                    try:
                        os.chmod(tmp, 0o600)
                    except OSError:
                        pass
                os.replace(tmp, path)
                self._fsync_dir(path.parent)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
            return
        try:
            fd = os.open(directory, os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
