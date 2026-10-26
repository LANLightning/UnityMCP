"""Unit tests for the release builder: version readers, bundle assembly, and
the localization-placeholder / fingerprint contracts. Fast — does not invoke a
full PEP 517 build. See spec §6.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

build_release = pytest.importorskip("build_release")

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "packaging" / "templates"


def test_read_pyproject_version():
    v = build_release.read_pyproject_version(REPO / "pyproject.toml")
    # Assert the reader returns a semver-shaped string (MAJOR.MINOR.PATCH, numeric
    # parts) rather than pinning a literal — a version bump must not break this.
    parts = v.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), v


def test_read_bridge_version():
    pkg = REPO / "unity-bridge" / "com.local.unitymcp-bridge" / "package.json"
    v = build_release.read_bridge_version(pkg)
    assert v == "0.8.0"


def test_templates_have_placeholders():
    env_example = (TEMPLATES / ".env.example").read_text(encoding="utf-8")
    assert "<UNITY_PROJECT_ROOT>" in env_example
    snippet = (TEMPLATES / "claude.json.snippet").read_text(encoding="utf-8")
    assert "<FILL_IN" in snippet


def test_templates_have_no_real_devpath():
    for name in (".env.example", "claude.json.snippet", "install.ps1", "install.sh", "DEPLOY.md"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "EXAMPLE_DEVELOPER_IDENTIFIER" not in text and "EXAMPLE_MACHINE_PREFIX" not in text, f"fingerprint in {name}"


def test_assemble_bundle_collects_seven_files(tmp_path):
    # Fake wheel + tgz so we don't run a real build.
    wheel = tmp_path / "unity_mcp_py-0.8.0-py3-none-any.whl"
    wheel.write_bytes(b"fake wheel")
    tgz = tmp_path / "com.local.unitymcp-bridge-0.7.0.tgz"
    tgz.write_bytes(b"fake tgz")
    out_dir = tmp_path / "out"

    zip_path = build_release.assemble_bundle(
        version="0.8.0",
        wheel=wheel,
        tgz=tgz,
        templates_dir=TEMPLATES,
        out_dir=out_dir,
    )
    assert zip_path.exists() and zip_path.suffix == ".zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = {Path(n).name for n in zf.namelist()}
    expected = {
        wheel.name,
        tgz.name,
        ".env.example",
        "claude.json.snippet",
        "install.ps1",
        "install.sh",
        "DEPLOY.md",
    }
    assert expected <= names, f"missing: {expected - names}"
