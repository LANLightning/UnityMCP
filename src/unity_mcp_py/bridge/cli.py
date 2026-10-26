"""Command-line administration for UnityMCP bridge endpoints."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any, Sequence

from .control import (
    BridgeBindingStore,
    BridgeControlClient,
    diagnose_endpoint,
    inspect_endpoint,
    read_wedge_evidence,
    repair_endpoint,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unity-mcp-py bridge",
        description="Pair, inspect, diagnose, clear, and repair Unity bridge endpoints.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pair = subparsers.add_parser(
        "pair", help="Persist an explicit MCP client <-> Unity bridge pair."
    )
    pair.add_argument("--client-id", required=True)
    _add_endpoint(pair)
    pair.add_argument(
        "--bridge-id", required=True, help="Expected Unity bridge identity."
    )

    status = subparsers.add_parser("status", help="Show listener, TCP owners, and accepted client.")
    _add_endpoint(status)
    status.add_argument("--client-id", help="Also show the stored pair for this MCP client.")

    owners = subparsers.add_parser("owners", help="Show OS owners of both TCP endpoint directions.")
    _add_endpoint(owners)

    diagnose = subparsers.add_parser("diagnose", help="Classify why an endpoint is unavailable or stuck.")
    _add_endpoint(diagnose)

    clear = subparsers.add_parser("clear", help="Clear a stored pair, accepted client, or listener.")
    clear.add_argument("--side", required=True, choices=("binding", "accepted", "listener"))
    clear.add_argument("--client-id")
    _add_endpoint(clear, required=False)
    clear.add_argument("--bridge-id", help="Required guard when restarting a listener.")

    repair = subparsers.add_parser("repair", help="Attempt a guarded transport recovery and re-diagnose.")
    _add_endpoint(repair)
    repair.add_argument("--client-id")
    repair.add_argument("--bridge-id")
    repair.add_argument(
        "--force",
        action="store_true",
        help="Allow disconnecting an active client with a different client ID; never kills a process.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        result, exit_code = _dispatch(args)
    except ValueError as exc:
        result = {"success": False, "errorCode": "INVALID_ARGUMENT", "error": str(exc)}
        exit_code = 2
    # Administrative output is frequently redirected or consumed from a legacy
    # Windows code page. ASCII-escaped JSON avoids an otherwise surprising
    # UnicodeEncodeError for project paths or process names outside that code page.
    print(json.dumps(result, indent=2, ensure_ascii=True, sort_keys=True))
    return exit_code


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "pair":
        if not args.client_id or not args.bridge_id:
            return _error(
                "EXPECTED_ID_REQUIRED",
                "client_id and bridge_id are required for an explicit pair",
            )
        binding = _store().pair(
            args.client_id, args.host, args.port, args.bridge_id
        )
        return {
            "success": True,
            "binding": asdict(binding),
            "nextStep": (
                f"Start the MCP process with UNITY_MCP_CLIENT_ID={binding.client_id!r}; "
                "the binding is applied when its bridge session is created."
            ),
        }, 0

    if args.command == "owners":
        return {"success": True, "socket": inspect_endpoint(args.host, args.port).to_dict()}, 0

    if args.command == "status":
        snapshot = inspect_endpoint(args.host, args.port)
        control = BridgeControlClient(args.host, args.port).status()
        binding = _store().get(args.client_id) if args.client_id else None
        return {
            "success": True,
            "socket": snapshot.to_dict(),
            "control": control,
            "wedgeEvidence": read_wedge_evidence(args.port, snapshot.listener_pid),
            "binding": asdict(binding) if binding else None,
        }, 0

    if args.command == "diagnose":
        snapshot = inspect_endpoint(args.host, args.port)
        control = BridgeControlClient(args.host, args.port).status()
        if not control.get("success"):
            wedge = read_wedge_evidence(args.port, snapshot.listener_pid)
            if wedge is not None:
                control = {**control, **wedge, "wedgeActive": True}
        diagnosis = diagnose_endpoint(snapshot, control)
        return {"success": True, "diagnosis": diagnosis.to_dict()}, 0

    if args.command == "clear":
        if args.side == "binding":
            if not args.client_id:
                return _error("CLIENT_ID_REQUIRED", "--client-id is required for side=binding")
            store = _store()
            current = store.get(args.client_id)
            if current is None:
                return {"success": True, "changed": False, "clientId": args.client_id}, 0
            if args.bridge_id and current.expected_bridge_id != args.bridge_id:
                return _error(
                    "STALE_TARGET",
                    "The stored bridge identity changed before clear.",
                )
            changed = store.clear_if_current(args.client_id, current.revision)
            if not changed:
                return _error(
                    "STALE_TARGET",
                    "The binding revision changed before clear.",
                )
            return {"success": True, "changed": changed, "clientId": args.client_id}, 0
        if args.port is None:
            return _error("PORT_REQUIRED", "--port is required for accepted/listener clear")
        if args.side == "listener":
            if not args.bridge_id:
                return _error(
                    "EXPECTED_ID_REQUIRED",
                    "--bridge-id is required before restarting a listener",
                )
            result = BridgeControlClient(args.host, args.port).clear_listener(args.bridge_id)
        else:
            if not args.client_id or not args.bridge_id:
                return _error(
                    "EXPECTED_ID_REQUIRED",
                    "--client-id and --bridge-id are required before disconnecting an accepted client",
                )
            control_client = BridgeControlClient(args.host, args.port)
            status = control_client.status()
            if not status.get("success"):
                return status, 1
            if status.get("bridgeId") != args.bridge_id or status.get("clientId") != args.client_id:
                return _error(
                    "STALE_TARGET",
                    "The bridge or accepted client identity changed before clear.",
                )
            result = control_client.disconnect_receiver(
                expected_bridge_id=args.bridge_id,
                expected_bridge_instance_id=str(status.get("bridgeInstanceId")),
                expected_generation=int(status.get("connectionGeneration", -1)),
                expected_client_id=args.client_id,
                expected_client_instance_id=status.get("clientInstanceId"),
            )
        return result, 0 if result.get("success") else 1

    if args.command == "repair":
        if args.force and not args.bridge_id:
            return _error(
                "EXPECTED_ID_REQUIRED",
                "--bridge-id is required together with --force",
            )
        result = repair_endpoint(
            args.host,
            args.port,
            client_id=args.client_id,
            expected_bridge_id=args.bridge_id,
            force=args.force,
        )
        return result, 0 if result.get("repaired") else 1

    raise ValueError(f"unsupported command: {args.command}")


def run_admin_action(
    action: str,
    *,
    host: str = "127.0.0.1",
    port: int = 17873,
    client_id: str | None = None,
    bridge_id: str | None = None,
    side: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Programmatic entry used by the MCP bridge_admin tool."""
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    if action not in {"pair", "status", "owners", "diagnose", "clear", "repair"}:
        raise ValueError(f"unsupported bridge admin action: {action}")
    if action == "clear" and side not in {"binding", "accepted", "listener"}:
        raise ValueError("clear requires side=binding|accepted|listener")
    namespace = argparse.Namespace(
        command=action,
        host=host,
        port=port,
        client_id=client_id,
        bridge_id=bridge_id,
        side=side,
        force=force,
    )
    result, exit_code = _dispatch(namespace)
    return {**result, "exitCode": exit_code}


def _add_endpoint(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=required)


def _store() -> BridgeBindingStore:
    return BridgeBindingStore()


def _error(code: str, message: str) -> tuple[dict[str, Any], int]:
    return {"success": False, "errorCode": code, "error": message}, 2


if __name__ == "__main__":
    sys.exit(main())
