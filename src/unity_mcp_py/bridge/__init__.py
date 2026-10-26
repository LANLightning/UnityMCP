"""Bridge to Unity. Re-exports vendored bridge_client and human_collab.

These modules originated in ~/.claude/flow/lib (single source of truth for
ECC's Phase 3 TCP bridge client and Phase 4 human-collab gate). They are now
vendored under unity_mcp_py._vendor so the wheel is self-contained and can be
installed on machines without an ECC flow install. To resync from upstream,
re-run scripts/sync_from_flow.ps1 and re-build the wheel.
"""
from .._vendor import bridge_client, human_collab

__all__ = ["bridge_client", "human_collab"]
