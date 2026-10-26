"""Shared environment-variable parsing helpers for state modules."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("unity_mcp_py")


def _read_positive_int_env(name: str, default: int) -> int:
    """Read a positive int from env; fall back to default on missing/invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d is not positive; using default %d", name, value, default)
        return default
    return value
