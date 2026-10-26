"""Pack the Unity bridge into an npm-compatible UPM tarball.

UPM accepts a tarball with the following layout:

    <tarball.tgz>
    └── package/                      <-- root prefix MUST be 'package/'
        ├── package.json
        ├── Editor/
        ├── Tests.EditMode/
        ├── protocol.schema.json
        ├── README.md
        └── ...

Usage:
    python scripts/pack_unity_bridge.py
    python scripts/pack_unity_bridge.py --src ./unity-bridge/com.local.unitymcp-bridge --out ./dist

The default source is ``unity-bridge/com.local.unitymcp-bridge/`` relative to
the repo root, and the default output directory is ``dist/``. The output
filename is derived from package.json (``{name}-{version}.tgz``).
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path


def pack(src: Path, out_dir: Path) -> Path:
    pkg_json_path = src / "package.json"
    if not pkg_json_path.is_file():
        raise SystemExit(f"package.json not found at {pkg_json_path}")

    meta = json.loads(pkg_json_path.read_text(encoding="utf-8"))
    name = meta["name"]
    version = meta["version"]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}-{version}.tgz"

    # Reproducible-ish: list files, sort, write with fixed mtime/uid/gid.
    files = [p for p in src.rglob("*") if p.is_file()]
    files.sort(key=lambda p: str(p.relative_to(src)).replace("\\", "/"))

    def _reset(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = 0
        ti.gid = 0
        ti.uname = ""
        ti.gname = ""
        ti.mtime = 0
        ti.mode = 0o644 if ti.isfile() else 0o755
        return ti

    with tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for f in files:
            rel = f.relative_to(src).as_posix()
            arcname = f"package/{rel}"
            tar.add(f, arcname=arcname, filter=_reset)

    return target


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--src",
        type=Path,
        default=repo_root / "unity-bridge" / "com.local.unitymcp-bridge",
        help="Path to the UPM package directory (containing package.json)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=repo_root / "dist",
        help="Output directory for the .tgz",
    )
    args = ap.parse_args()

    src = args.src.resolve()
    out_dir = args.out.resolve()
    target = pack(src, out_dir)

    size = target.stat().st_size
    print(f"Packed: {target}")
    print(f"Size:   {size:,} bytes ({size/1024:.1f} KB)")

    # Quick contents-list for sanity.
    with tarfile.open(target, "r:gz") as tar:
        members = tar.getmembers()
    print(f"Members: {len(members)}")
    for m in members[:5]:
        print(f"  {m.name}")
    if len(members) > 10:
        print(f"  ... ({len(members) - 5} more)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
