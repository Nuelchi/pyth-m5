from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path

from .ws_manager import WebSocketManager
from .strategy_loader import load_strategy
from .backtest_engine import run_backtest_task


app = FastAPI(title="Backtest Service", version="0.1.0")

# CORS for development convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static frontend
FRONTEND_DIR = str(Path(__file__).resolve().parents[1] / "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

ws_manager = WebSocketManager()


class BacktestRequest(BaseModel):
    symbol: Optional[str] = None
    symbols: Optional[List[str]] = None
    timeframe: str = Field("H1", description="Timeframe code, e.g. H1, M15, D1")
    source: Literal["yfinance", "mt5"] = Field("yfinance")
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    # Advanced knobs are ignored server-side; hardcoded for consistency with BCT
    cash: float = 100000.0
    commission: float = 0.001
    slippage: float = 0.0
    size_percent: float = 90.0
    sizing_mode: Literal["percent", "lots", "risk"] = Field("percent")
    lots_per_trade: Optional[float] = 1.0
    include_buy_hold: bool = True
    risk_percent: float = 0.01
    stop_loss_pips: int = 50
    lot_multiplier: float = 1.0
    leverage: float = 1.0
    stream_delay_ms: int = 30
    fast: Optional[bool] = False
    strategy_name: Optional[str] = Field(None, description="Optional name of strategy in strategies folder")
    strategy_code: Optional[str] = Field(None, description="Optional Python code defining a bt.Strategy subclass")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("frontend/index.html")


@app.post("/backtest")
async def start_backtest(req: BacktestRequest) -> JSONResponse:
    run_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()

    # Resolve symbols list (single-asset by preference)
    symbols_list = req.symbols or ([req.symbol] if req.symbol else [])
    if not symbols_list:
        return JSONResponse(status_code=400, content={"error": "symbol is required"})

    sanitized = [s for s in symbols_list if s and s.strip()]
    if not sanitized:
        return JSONResponse(status_code=400, content={"error": "symbol is required"})

    now = datetime.utcnow()
    start_dt = req.start or (now - timedelta(days=90))
    end_dt = req.end or now
    symbols_list = sanitized

    try:
        strategy_cls = load_strategy(code=req.strategy_code, name=req.strategy_name)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": f"Strategy load failed: {exc}"})

    # Fire-and-forget task
    # Hardcode core backtest knobs to match BCT-like defaults regardless of client inputs
    asyncio.create_task(
        run_backtest_task(
            run_id=run_id,
            loop=loop,
            ws_manager=ws_manager,
            symbols=symbols_list,
            timeframe=req.timeframe,
            source=req.source,
            start=start_dt,
            end=end_dt,
            initial_cash=100000.0,
            commission=0.001,          # 10 bps
            slippage=0.0,
            size_percent=90.0,          # 90% of cash per trade
            sizing_mode="percent",
            lots_per_trade=1.0,
            include_buy_hold=True,
            risk_percent=0.01,
            stop_loss_pips=50,
            lot_multiplier=1.0,
            leverage=1.0,
            stream_delay_ms=(8 if (req.fast or False) else 30),
            strategy_cls=strategy_cls,
        )
    )

    return JSONResponse({"run_id": run_id})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, run_id: str = Query(...)) -> None:
    await ws_manager.register(run_id, websocket)
    try:
        while True:
            # Keep the connection open; client may send pings or noop messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.unregister(run_id, websocket)
