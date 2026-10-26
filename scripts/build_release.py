"""Build the unity-mcp deployment bundle.

Pipeline:
  1. (default) run pytest as a green-light gate
  2. build the Python wheel via `python -m build --wheel`
  3. pack the Unity bridge .tgz via scripts/pack_unity_bridge.py
  4. assemble wheel + tgz + packaging/templates/* into
     dist/unity-mcp-deploy-<pyver>/ and zip it

Usage:
  python scripts/build_release.py
  python scripts/build_release.py --skip-tests      # skip the green gate (dev)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "packaging" / "templates"
DIST = REPO / "dist"
_TEMPLATE_NAMES = (".env.example", "claude.json.snippet", "install.ps1", "install.sh", "DEPLOY.md")


def read_pyproject_version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["version"]


def read_bridge_version(package_json: Path) -> str:
    return json.loads(package_json.read_text(encoding="utf-8"))["version"]


def assemble_bundle(
    *, version: str, wheel: Path, tgz: Path, templates_dir: Path, out_dir: Path
) -> Path:
    """Copy wheel + tgz + templates into out_dir/unity-mcp-deploy-<version>/ and zip it.

    Returns the path to the created .zip.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = out_dir / f"unity-mcp-deploy-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    shutil.copy2(wheel, stage / wheel.name)
    shutil.copy2(tgz, stage / tgz.name)
    for name in _TEMPLATE_NAMES:
        src = templates_dir / name
        if not src.is_file():
            raise SystemExit(f"template missing: {src}")
        shutil.copy2(src, stage / name)

    zip_path = out_dir / f"unity-mcp-deploy-{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(out_dir).as_posix())
    return zip_path


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(REPO))
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd)}")


def _find_one(pattern: str) -> Path:
    matches = sorted(DIST.glob(pattern))
    if not matches:
        raise SystemExit(f"expected artifact not found in dist/: {pattern}")
    return matches[-1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-tests", action="store_true", help="skip the pytest green gate")
    args = ap.parse_args(argv)

    py_version = read_pyproject_version(REPO / "pyproject.toml")
    bridge_pkg = REPO / "unity-bridge" / "com.local.unitymcp-bridge" / "package.json"
    bridge_version = read_bridge_version(bridge_pkg)
    print(f"Python version: {py_version} | bridge version: {bridge_version}")

    if not args.skip_tests:
        _run([sys.executable, "-m", "pytest", "-q"])

    DIST.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(DIST)])
    _run([sys.executable, str(REPO / "scripts" / "pack_unity_bridge.py")])

    wheel = _find_one(f"unity_mcp_py-{py_version}-*.whl")
    tgz = _find_one(f"com.local.unitymcp-bridge-{bridge_version}.tgz")

    zip_path = assemble_bundle(
        version=py_version, wheel=wheel, tgz=tgz, templates_dir=TEMPLATES, out_dir=DIST
    )
    size = zip_path.stat().st_size
    print(f"\n[OK] bundle: {zip_path} ({size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
