from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .spec_runner import parse_spec, compile_strategy
from .strategy_loader import load_strategy
from .backtest_engine import run_backtest_task
from .ws_manager import WebSocketManager
import os
import re
import requests


router = APIRouter(prefix="/agent", tags=["agent"])


class AgentRunRequest(BaseModel):
    goal: str = Field(..., description="High-level intent, e.g. 'Backtest EMA cross on EURUSD' ")
    mode: Literal["code", "spec"] = Field("code")
    # code mode
    strategy_code: Optional[str] = None
    # spec mode
    spec_yaml: Optional[str] = None
    spec_obj: Optional[dict] = None
    # run params
    symbol: Optional[str] = "EURUSD=X"
    timeframe: Optional[str] = "H1"
    source: Literal["yfinance", "mt5", "oanda"] = "yfinance"
    fast: bool = True
    # retry budget for future extensions
    max_retries: int = 0


class AgentRunResponse(BaseModel):
    plan: list[dict]
    run_id: Optional[str] = None
    strategy: Optional[str] = None
    message: Optional[str] = None


_ws_manager = WebSocketManager()


@router.post("/run", response_model=AgentRunResponse)
async def agent_run(req: AgentRunRequest) -> AgentRunResponse:
    # Build a simple plan description the UI can show
    plan: list[dict] = [
        {"id": "plan", "label": "Create execution plan", "status": "done"},
    ]
    if req.mode == "spec":
        plan += [
            {"id": "compile", "label": "Compile YAML spec to strategy", "status": "pending"},
            {"id": "backtest", "label": "Start backtest and stream metrics", "status": "pending"},
        ]
    else:
        plan += [
            {"id": "validate", "label": "Validate Python bt.Strategy class", "status": "pending"},
            {"id": "backtest", "label": "Start backtest and stream metrics", "status": "pending"},
        ]

    # Step: compile/validate
    if req.mode == "spec":
        plan[1]["status"] = "running"
        try:
            parsed = parse_spec(spec_yaml=req.spec_yaml, spec_obj=req.spec_obj)
            strategy_name, strategy_cls = compile_strategy(parsed)
        except Exception as exc:  # noqa: BLE001
            plan[1]["status"] = "error"
            return AgentRunResponse(plan=plan, message=f"Spec error: {exc}")
        plan[1]["status"] = "done"
    else:
        plan[1]["status"] = "running"
        try:
            # Will raise if invalid code
            _ = load_strategy(code=req.strategy_code, name=None)
            strategy_name, strategy_cls = ("python_strategy", _)
        except Exception as exc:  # noqa: BLE001
            plan[1]["status"] = "error"
            return AgentRunResponse(plan=plan, message=f"Strategy validation failed: {exc}")
        plan[1]["status"] = "done"

    # Step: backtest
    backtest_step_index = 2
    plan[backtest_step_index]["status"] = "running"
    run_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    now = datetime.utcnow()
    start_dt = now - timedelta(days=180)
    end_dt = now
    asyncio.create_task(
        run_backtest_task(
            run_id=run_id,
            loop=loop,
            ws_manager=_ws_manager,
            symbols=[req.symbol or "EURUSD=X"],
            timeframe=req.timeframe or "H1",
            source=req.source,
            start=start_dt,
            end=end_dt,
            initial_cash=100000.0,
            commission=0.001,
            slippage=0.0,
            size_percent=90.0,
            sizing_mode="percent",
            lots_per_trade=1.0,
            include_buy_hold=True,
            risk_percent=0.01,
            stop_loss_pips=50,
            lot_multiplier=1.0,
            leverage=1.0,
            stream_delay_ms=(8 if (req.fast) else 30),
            strategy_cls=strategy_cls,
        )
    )
    plan[backtest_step_index]["status"] = "done"
    return AgentRunResponse(plan=plan, run_id=run_id, strategy=strategy_name)


# -------- Auto-fix and retry ----------

class FixRunRequest(BaseModel):
    error: str
    code: str
    symbol: Optional[str] = "EURUSD=X"
    timeframe: Optional[str] = "H1"
    source: Literal["yfinance", "mt5", "oanda"] = "yfinance"
    fast: bool = True
    max_retries: int = 2


class FixRunResponse(BaseModel):
    run_id: Optional[str] = None
    attempts: int
    fixed_code: Optional[str] = None
    message: Optional[str] = None


def _taxonomy_fix(original: str, error: str) -> str:
    code = original
    # Remove common invalid attributes
    code = re.sub(r"\.isclosed\b", "", code)  # Orders don't have isclosed
    # Replace invalid .point usage:
    # - For data feeds like self.data / self.data1 → use close[0]
    code = re.sub(r"\bself\.(data\d*)\.point\b", r"self.\1.close[0]", code)
    # - For indicator/line series (e.g., self.ema) → use [0]
    code = re.sub(r"\bself\.([A-Za-z_]\w*)\.point\b", r"self.\1[0]", code)
    # Ensure self.order lifecycle exists
    if "def notify_order" not in code:
        notify_stub = (
            "\n    def notify_order(self, order):\n"
            "        if order.status in [order.Completed, order.Canceled, order.Margin, order.Rejected]:\n"
            "            self.order = None\n"
        )
        code = re.sub(r"def __init__\(self\):\n", lambda m: m.group(0) + "        self.order = None\n", code, count=1)
        code = code + notify_stub
    # Replace direct self.order.is... checks with None guard
    code = re.sub(r"if\s+self\.order\s*\.\w+", "if self.order:", code)
    return code


def _llm_fix(original: str, error: str) -> Optional[str]:
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("NEXT_PUBLIC_GROQ_API_KEY")
    if not api_key:
        return None
    try:
        prompt = (
            "Fix the following Python Backtrader bt.Strategy so it avoids the error. "
            "Return ONLY a single valid bt.Strategy class.\n\nError:\n" + error + "\n\nCode:\n" + original
        )
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "messages": [
                    {"role": "system", "content": "You rewrite code into a single valid Backtrader bt.Strategy class only."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 1000,
                "stream": False,
            },
            timeout=25,
        )
        if not resp.ok:
            return None
        data = resp.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not text:
            return None
        m = re.search(r"```python([\s\S]*?)```", text, re.IGNORECASE)
        fixed = (m.group(1) if m else text).strip()
        return fixed
    except Exception:
        return None


@router.post("/fix-run", response_model=FixRunResponse)
async def agent_fix_and_run(req: FixRunRequest) -> FixRunResponse:
    attempts = 0
    candidate = req.code
    message = None
    while attempts <= max(0, int(req.max_retries)):
        attempts += 1
        try:
            # First attempt: taxonomy; later attempts: LLM if available
            if attempts == 1:
                candidate = _taxonomy_fix(candidate, req.error)
            else:
                llm = _llm_fix(candidate, req.error)
                candidate = llm or _taxonomy_fix(candidate, req.error)

            strategy_cls = load_strategy(code=candidate, name=None)
            run_id = uuid.uuid4().hex
            loop = asyncio.get_running_loop()
            now = datetime.utcnow()
            start_dt = now - timedelta(days=180)
            end_dt = now
            asyncio.create_task(
                run_backtest_task(
                    run_id=run_id,
                    loop=loop,
                    ws_manager=_ws_manager,
                    symbols=[req.symbol or "EURUSD=X"],
                    timeframe=req.timeframe or "H1",
                    source=req.source,
                    start=start_dt,
                    end=end_dt,
                    initial_cash=100000.0,
                    commission=0.001,
                    slippage=0.0,
                    size_percent=90.0,
                    sizing_mode="percent",
                    lots_per_trade=1.0,
                    include_buy_hold=True,
                    risk_percent=0.01,
                    stop_loss_pips=50,
                    lot_multiplier=1.0,
                    leverage=1.0,
                    stream_delay_ms=(8 if (req.fast) else 30),
                    strategy_cls=strategy_cls,
                )
            )
            return FixRunResponse(run_id=run_id, attempts=attempts, fixed_code=candidate)
        except Exception as exc:  # noqa: BLE001
            message = f"Fix attempt {attempts} failed: {exc}"
            continue
    return FixRunResponse(run_id=None, attempts=attempts, fixed_code=candidate, message=message)

