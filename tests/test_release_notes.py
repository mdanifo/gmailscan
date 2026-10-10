"""The release workflow's one script: CHANGELOG entry in, release notes out."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"

CHANGELOG = """# Changelog

Pin a tag.

## v0.9.0 (2026-10-10)

Fixes

- A long bullet that the CHANGELOG wraps at eighty columns, continuing on a
  second line and
  a third.
- A short one.

A paragraph of prose,
wrapped once.

Breaks

- Nothing.

## v0.8.0 (2026-10-01)

- Older.
"""


def _load():
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_entry_is_one_release_unwrapped():
    body = _load().entry(CHANGELOG, "0.9.0")
    assert body.startswith("Fixes\n\n- A long bullet")
    assert "second line and a third." in body  # the wrapped bullet is one line again
    assert "A paragraph of prose, wrapped once." in body
    assert "Older" not in body  # the next release's entry is not ours


def test_notes_lead_with_both_ways_to_pin():
    text = _load().notes(CHANGELOG, "v0.9.0")
    assert "releases/download/v0.9.0/gmailscan-0.9.0-py3-none-any.whl" in text
    assert "gmailscan@v0.9.0" in text
    assert text.rstrip().endswith("blob/v0.9.0/CHANGELOG.md)")


def test_a_version_without_an_entry_fails_the_release():
    with pytest.raises(SystemExit, match="no entry for v0.7.0"):
        _load().entry(CHANGELOG, "0.7.0")


def test_the_real_changelog_has_an_entry_for_the_declared_version():
    """What the workflow checks on a tag, checked on every commit instead: the
    version in __init__ has a CHANGELOG entry, so a tag cannot ship without one."""
    import gmailscan

    result = subprocess.run(
        [sys.executable, str(SCRIPT), f"v{gmailscan.__version__}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert f"gmailscan-{gmailscan.__version__}-py3-none-any.whl" in result.stdout
