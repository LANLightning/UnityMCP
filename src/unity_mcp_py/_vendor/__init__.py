"""Vendored copies of bridge_client.py and human_collab.py.

Originally maintained at ~/.claude/flow/lib/ in the ECC flow install. Copied
here so the wheel is self-contained and can be installed on machines without
that directory. Resync by re-running scripts/sync_from_flow.ps1.

Do not import these modules directly from application code — go through
unity_mcp_py.bridge, which re-exports the public surface.
"""
