from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Type

import yaml


@dataclass
class ParsedSpec:
	name: str
	symbol: str
	timeframe: str
	engine: str
	backtest: Dict[str, Any]
	entry: Dict[str, Any]
	risk: Dict[str, Any]


def _ensure_timeframe(tf: str) -> str:
	# Normalize common lowercase formats to engine format used in data_loader
	upper = (tf or "").strip().upper()
	mapping = {
		"1M": "M1",
		"5M": "M5",
		"15M": "M15",
		"30M": "M30",
		"1H": "H1",
		"4H": "H4",
		"1D": "D1",
	}
	return mapping.get(upper, upper)


def parse_spec(spec_yaml: Optional[str] = None, spec_obj: Optional[Dict[str, Any]] = None) -> ParsedSpec:
	if not spec_obj and not spec_yaml:
		raise ValueError("spec_yaml or spec_obj is required")
	data: Dict[str, Any] = spec_obj or yaml.safe_load(spec_yaml or "{}")
	if not isinstance(data, dict):
		raise ValueError("Invalid spec: expected mapping at root")
	name = str(data.get("name") or "strategy")
	symbol = str(data.get("symbol") or data.get("symbols") or "EURUSD")
	timeframe = _ensure_timeframe(str(data.get("timeframe") or "M30"))
	engine = str((data.get("backtest") or {}).get("engine") or data.get("engine") or "backtrader").lower()
	entry = dict(data.get("entry") or {})
	risk = dict(data.get("risk") or {})
	backtest = dict(data.get("backtest") or {})
	return ParsedSpec(name=name, symbol=symbol, timeframe=timeframe, engine=engine, backtest=backtest, entry=entry, risk=risk)


def build_backtrader_strategy(spec: ParsedSpec) -> Type:  # returns a bt.Strategy subclass
	import backtrader as bt

	entry_type = str(spec.entry.get("type") or "ema_cross").lower()
	fast = int(spec.entry.get("fast") or spec.entry.get("fast_period") or 9)
	slow = int(spec.entry.get("slow") or spec.entry.get("slow_period") or 21)
	use_atr = (str(spec.risk.get("stop") or "").lower() == "atr")
	atr_period = int(spec.risk.get("atr_period") or 14)
	r_multiple = float(spec.risk.get("r_multiple") or 1.5)

	class EMACrossWithRisk(bt.Strategy):
		params = dict(
			fast=fast,
			slow=slow,
			atr_period=atr_period,
			r_multiple=r_multiple,
			use_atr=use_atr,
		)

		def __init__(self):
			self.fast = bt.ind.EMA(period=self.p.fast)
			self.slow = bt.ind.EMA(period=self.p.slow)
			self.crossover = bt.ind.CrossOver(self.fast, self.slow)
			self.atr = bt.ind.ATR(period=self.p.atr_period)
			self.entry_price = None

		def next(self):
			if not self.position:
				if self.crossover > 0:
					# Long entry
					size = 1  # sizing is controlled by external sizer
					self.buy(size=size)
					self.entry_price = self.data.close[0]
					if self.p.use_atr and self.atr[0] > 0:
						stop = self.entry_price - (self.p.r_multiple * self.atr[0])
						self.sell(exectype=bt.Order.Stop, price=stop)
				elif self.crossover < 0:
					# Short entry (optional). Keep long-only for simplicity; close if any
					pass
			else:
				# Exit on cross-under
				if self.crossover < 0:
					self.close()

	return EMACrossWithRisk


def compile_strategy(spec: ParsedSpec) -> Tuple[str, Type]:
	if spec.engine != "backtrader":
		raise ValueError(f"Unsupported engine: {spec.engine}")
	strategy_cls = build_backtrader_strategy(spec)
	return spec.name, strategy_cls

