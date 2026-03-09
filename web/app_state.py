"""web/app_state.py — Shared in-memory SSE state for ongoing project runs."""
from __future__ import annotations

import asyncio

# Active queues for ongoing runs
_run_queues: dict[str, asyncio.Queue] = {}
# Stored events for completed/replayed runs
_run_events: dict[str, list[dict]] = {}
