"""Guards H2: no script under scripts/ may hardcode the dev-machine PROJECT_EDITOR path.
See spec §3 H2.
"""
from __future__ import annotations

from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def test_no_devpath_fingerprint_in_any_script():
    offenders = []
    for py in _SCRIPTS_DIR.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for needle in ("EXAMPLE_DEVELOPER_IDENTIFIER", "EXAMPLE_MACHINE_PREFIX"):
            if needle in text:
                offenders.append(f"{py.name}: {needle}")
    assert not offenders, f"dev fingerprint in scripts: {offenders}"
