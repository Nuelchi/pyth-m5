from __future__ import annotations

import asyncio
from typing import Dict, Set

from fastapi import WebSocket


class WebSocketManager:
	def __init__(self) -> None:
		self._clients_by_run: Dict[str, Set[WebSocket]] = {}
		self._lock = asyncio.Lock()

	async def register(self, run_id: str, websocket: WebSocket) -> None:
		await websocket.accept()
		async with self._lock:
			if run_id not in self._clients_by_run:
				self._clients_by_run[run_id] = set()
			self._clients_by_run[run_id].add(websocket)

	async def unregister(self, run_id: str, websocket: WebSocket) -> None:
		async with self._lock:
			if run_id in self._clients_by_run and websocket in self._clients_by_run[run_id]:
				self._clients_by_run[run_id].remove(websocket)
				if not self._clients_by_run[run_id]:
					del self._clients_by_run[run_id]

	async def broadcast(self, run_id: str, message: dict) -> None:
		# Send to a snapshot of current clients to avoid mutation during iteration
		async with self._lock:
			targets = list(self._clients_by_run.get(run_id, set()))
		for ws in targets:
			try:
				await ws.send_json(message)
			except Exception:
				# Ignore send errors; client may have disconnected unexpectedly
				pass
