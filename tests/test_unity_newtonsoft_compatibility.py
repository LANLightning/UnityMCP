"""Unity's bundled Newtonsoft API does not support no-arg JToken.Value<T>()."""
from __future__ import annotations

import re
from pathlib import Path


def test_bridge_avoids_unity_incompatible_no_arg_jtoken_value() -> None:
    package = Path("unity-bridge/com.local.unitymcp-bridge")
    incompatible: list[str] = []
    pattern = re.compile(r"\.Value<[^>]+>\(\)")

    for source in package.rglob("*.cs"):
        for line_number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                incompatible.append(f"{source}:{line_number}: {line.strip()}")

    assert incompatible == [], "\n".join(incompatible)
