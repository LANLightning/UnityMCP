"""CLI contract for bridge pairing, endpoint cleanup, diagnosis, and repair."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from unity_mcp_py.bridge import cli
from unity_mcp_py.bridge.control import EndpointSnapshot


@pytest.fixture(autouse=True)
def auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNITY_MCP_AUTH_TOKEN", "t" * 32)


def test_pair_command_persists_named_client_and_bridge(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("UNITY_MCP_BINDINGS_FILE", str(tmp_path / "bindings.json"))

    exit_code = cli.main(
        [
            "pair",
            "--client-id",
            "codex-project_fixture",
            "--port",
            "17874",
            "--bridge-id",
            "game-b",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["success"] is True
    assert payload["binding"]["client_id"] == "codex-project_fixture"
    assert payload["binding"]["expected_bridge_id"] == "game-b"


def test_status_reports_socket_owners_and_control_owner(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "inspect_endpoint",
        lambda host, port: EndpointSnapshot(host, port, listener_pid=101),
    )
    monkeypatch.setattr(
        cli.BridgeControlClient,
        "status",
        lambda self: {
            "responsive": True,
            "clientConnected": True,
            "clientId": "codex-project_fixture",
            "bridgeId": "game-a",
        },
    )

    exit_code = cli.main(["status", "--port", "17873"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["socket"]["listener_pid"] == 101
    assert payload["control"]["clientId"] == "codex-project_fixture"


def test_clear_requires_target_identity_for_listener_restart(capsys) -> None:
    exit_code = cli.main(["clear", "--side", "listener", "--port", "17873"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["errorCode"] == "EXPECTED_ID_REQUIRED"


def test_force_repair_requires_bridge_identity(capsys) -> None:
    exit_code = cli.main(["repair", "--port", "17873", "--client-id", "codex-project_fixture", "--force"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["errorCode"] == "EXPECTED_ID_REQUIRED"


def test_server_entrypoint_delegates_bridge_subcommand(monkeypatch) -> None:
    from unity_mcp_py import server

    calls: list[list[str]] = []
    monkeypatch.setattr("sys.argv", ["unity-mcp-py", "bridge", "diagnose", "--port", "17873"])
    monkeypatch.setattr(cli, "main", lambda args: calls.append(args) or 0)

    server.main()

    assert calls == [["diagnose", "--port", "17873"]]
