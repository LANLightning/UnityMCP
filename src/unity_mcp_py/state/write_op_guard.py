"""Write-op token tracker. See spec §6."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable

GC_INTERVAL_SECONDS = 300       # GC scan every 5 min
TOKEN_TTL_SECONDS = 86400       # 24h


@dataclass
class WriteContext:
    """Represents an outstanding write operation awaiting observation."""

    token: str
    tool_name: str
    args_summary: str
    started_at: float
    line_number_before: int
    expires_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.expires_at = self.started_at + TOKEN_TTL_SECONDS


@dataclass
class ConsumeResult:
    """Result of a consume() call: categorizes each token by outcome."""

    consumed: list[str]
    expired: list[str]
    unknown: list[str]


class WriteOpGuard:
    """Process-local token store for write-op pairing. No persistence across restarts."""

    def __init__(self) -> None:
        self._tokens: dict[str, WriteContext] = {}
        self._lock = asyncio.Lock()

    async def issue(
        self,
        tool_name: str,
        args_summary: str,
        line_number_before: int,
    ) -> WriteContext:
        """Issue a new write-op token and register it in the store."""
        async with self._lock:
            token = str(uuid.uuid4())
            ctx = WriteContext(
                token=token,
                tool_name=tool_name,
                args_summary=args_summary,
                started_at=time.time(),
                line_number_before=line_number_before,
            )
            self._tokens[token] = ctx
            return ctx

    async def consume(self, tokens: Iterable[str]) -> ConsumeResult:
        """Mark tokens as consumed. Returns categorized outcome for each token."""
        async with self._lock:
            now = time.time()
            consumed: list[str] = []
            expired: list[str] = []
            unknown: list[str] = []
            for t in tokens:
                ctx = self._tokens.get(t)
                if ctx is None:
                    unknown.append(t)
                    continue
                if ctx.expires_at < now:
                    expired.append(t)
                    del self._tokens[t]
                    continue
                consumed.append(t)
                del self._tokens[t]
            return ConsumeResult(consumed=consumed, expired=expired, unknown=unknown)

    async def list_pending(self) -> list[WriteContext]:
        """Return all currently pending (un-consumed) write contexts."""
        async with self._lock:
            return list(self._tokens.values())

    async def gc_once(self) -> int:
        """Reap expired tokens. Returns number of tokens reaped."""
        async with self._lock:
            now = time.time()
            stale = [t for t, ctx in self._tokens.items() if ctx.expires_at < now]
            for t in stale:
                del self._tokens[t]
            return len(stale)

    async def gc_loop(self) -> None:
        """Background coroutine: runs gc_once every GC_INTERVAL_SECONDS."""
        while True:
            await asyncio.sleep(GC_INTERVAL_SECONDS)
            await self.gc_once()


# Module-level singleton (process-wide)
GUARD = WriteOpGuard()
