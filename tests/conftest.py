"""Shared pytest fixtures for the plugin test suite."""

from __future__ import annotations

import itertools
import os
import shutil
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

_COUNTER = itertools.count()


def _tmp_base() -> Path:
    """Return a writable temp base outside the plugin repository.

    The Windows sandbox used during development denies directory creation below
    the AstrBot checkout, so tests create their scratch data next to the
    workspace root instead of using the system temp directory.
    """
    override = os.environ.get("PLUGIN_TEST_TMP")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    workspace_root = here.parents[5] if len(here.parents) > 5 else here.parent
    return workspace_root / ".dsh-test-tmp"


@pytest.fixture()
def data_root() -> Path:
    """Provide a unique, writable directory for one test case."""
    base = _tmp_base()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"case-{os.getpid()}-{next(_COUNTER)}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
