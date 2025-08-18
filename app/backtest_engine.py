from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Type

import backtrader as bt
import pandas as pd

from .data_loader import load_yfinance, load_mt5, load_oanda
from .ws_manager import WebSocketManager


class PandasData(bt.feeds.PandasData):
	params = (
		("datetime", None),
		("open", "open"),
		("high", "high"),
		("low", "low"),
		("close", "close"),
		("volume", "volume"),
	)


class FlexibleSizer(bt.Sizer):
	"""Unified sizer supporting percent, lots, and risk-based sizing.

	Params:
	- mode: one of {"percent", "lots", "risk"}
	- percent: position size as percent of available cash
	- lots: number of lots per trade
	- risk: fraction of cash to risk per trade (e.g. 0.01 = 1%)
	- stop_loss_pips: assumed stop distance in pips for risk sizing
	- lot_multiplier: units per lot (100000 typical for FX)
	"""
	params = dict(
		mode="percent",
		percent=90.0,
		lots=1.0,
		risk=0.01,
		stop_loss_pips=50,
		lot_multiplier=100000.0,
		leverage=1.0,
	)

	def _getsizing(self, comminfo, cash, data, isbuy):
		price = float(data.close[0]) if len(data) else 0.0
		mode = str(self.p.mode or "percent").lower()
		if mode == "percent":
			if price <= 0.0:
				return 0
			units = (float(cash) * (float(self.p.percent) / 100.0)) / price
			return max(0, int(units))
		elif mode == "lots":
			units = float(self.p.lots) * float(self.p.lot_multiplier)
			# Clamp to affordable amount considering leverage to avoid silent order rejections
			if price > 0.0 and cash > 0.0:
				lev = float(getattr(self.p, 'leverage', 1.0) or 1.0)
				affordable = int((float(cash) * max(1.0, lev)) / float(price))
				if affordable <= 0:
					units = 0
				else:
					units = min(int(units), affordable)
			return max(0, int(units))
		elif mode == "risk":
			# Attempt to use broker's pip value if available; otherwise approximate
			pip_value = None
			try:
				pip_value = comminfo.pipvalue()
			except Exception:
				pip_value = None
			if not pip_value or pip_value <= 0:
				# Approximate using lot multiplier and a default pip size of 0.0001
				pip_value = 0.0001 * float(self.p.lot_multiplier)
			risk_amount = float(cash) * float(self.p.risk)
			per_unit_loss = max(1e-12, float(self.p.stop_loss_pips) * float(pip_value))
			units = risk_amount / per_unit_loss
			return max(0, int(units))
		return 0


def _load_df(symbol: str, source: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
	if source == "yfinance":
		return load_yfinance(symbol, timeframe, start, end)
	if source == "mt5":
		return load_mt5(symbol, timeframe, start, end)
	raise ValueError(f"Unsupported data source: {source}")


def _broadcast(loop: asyncio.AbstractEventLoop, ws_manager: WebSocketManager, run_id: str, message: Dict[str, Any]) -> None:
	asyncio.run_coroutine_threadsafe(ws_manager.broadcast(run_id, message), loop)


def _sanitize_json_numbers(obj: Any) -> Any:
	"""Recursively make NaN/Infinity JSON-safe: convert to 'nan'/'inf' strings so UI can display them."""
	try:
		import math  # local import to avoid top clutter
	except Exception:
		math = None  # type: ignore
	if isinstance(obj, dict):
		return {k: _sanitize_json_numbers(v) for k, v in obj.items()}
	if isinstance(obj, list):
		return [_sanitize_json_numbers(v) for v in obj]
	if isinstance(obj, float) and math is not None:
		if math.isnan(obj):
			return 'nan'
		if math.isinf(obj):
			return 'inf'
	return obj


async def run_backtest_task(
	run_id: str,
	loop: asyncio.AbstractEventLoop,
	ws_manager: WebSocketManager,
	symbols: List[str],
	timeframe: str,
	source: str,
	start: datetime,
	end: datetime,
	initial_cash: float,
	commission: float,
	slippage: float,
	size_percent: float,
	sizing_mode: str,
	lots_per_trade: float,
	include_buy_hold: bool,
	risk_percent: float,
	stop_loss_pips: int,
	lot_multiplier: float,
	leverage: float,
	stream_delay_ms: int,
	strategy_cls: Type[bt.Strategy],
) -> None:
	cerebro = bt.Cerebro()
	cerebro.broker.setcash(initial_cash)
	cerebro.broker.setcommission(commission=commission)
	# Apply percent slippage approximation
	if slippage and slippage > 0:
		cerebro.broker.set_slippage_perc(slippage)

	# BCT-like sizer: fixed percent of cash per trade
	try:
		cerebro.addsizer(bt.sizers.PercentSizer, percents=int(size_percent))
	except Exception:
		pass

	# Load data
	first_close_series = None
	for symbol in symbols:
		df = await loop.run_in_executor(None, _load_df, symbol, source, timeframe, start, end)
		if first_close_series is None and 'close' in df.columns and not df.empty:
			first_close_series = df['close'].copy()
		feed = PandasData(dataname=df)
		cerebro.adddata(feed, name=symbol)

	# Analyzers
	cerebro.addanalyzer(bt.analyzers.DrawDown, _name='dd')
	cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
	cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
	cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
	cerebro.addanalyzer(bt.analyzers.SQN, _name='sqn')

	# Wrap strategy to stream updates per bar
	outer_ws_manager = ws_manager
	outer_loop = loop
	outer_run_id = run_id
	trade_log = []

	class StrategyWrapper(strategy_cls):  # type: ignore[misc]
		def __init__(self):
			super().__init__()
			self._trade_log = trade_log
			# Prepare helpers for risk-based sizing
			self._atr = None
			# Provide a compatibility attribute for frameworks that expect 'self.orders'
			# Some translated/user strategies access 'self.orders' to manage open orders.
			# Backtrader does not define this by default, so we create and maintain it.
			if not hasattr(self, 'orders'):
				self.orders = []  # type: ignore[attr-defined]
			# Initialize common broker aliases expected by user/AI strategies
			try:
				self.broker.equity = float(self.broker.getvalue())  # type: ignore[attr-defined]
				self.broker.cash = float(self.broker.getcash())     # type: ignore[attr-defined]
				self.broker.value = float(self.broker.getvalue())   # type: ignore[attr-defined]
			except Exception:
				pass

		# Track orders created via buy/sell/close to keep 'self.orders' meaningful
		def buy(self, *args, **kwargs):  # type: ignore[override]
			order = super().buy(*args, **kwargs)
			try:
				self.orders.append(order)  # type: ignore[attr-defined]
			except Exception:
				pass
			return order

		def sell(self, *args, **kwargs):  # type: ignore[override]
			order = super().sell(*args, **kwargs)
			try:
				self.orders.append(order)  # type: ignore[attr-defined]
			except Exception:
				pass
			return order

		def close(self, *args, **kwargs):  # type: ignore[override]
			order = super().close(*args, **kwargs)
			try:
				self.orders.append(order)  # type: ignore[attr-defined]
			except Exception:
				pass
			return order

		def notify_order(self, order):
			if order.status == order.Completed:
				dt = bt.num2date(self.datas[0].datetime[0])
				# Classify action: if position is flat after execution, treat as 'close'
				pos_size = float(getattr(self.position, 'size', 0.0) or 0.0)
				action = 'buy' if order.isbuy() else 'sell'
				if pos_size == 0.0:
					action = 'close'
				entry = {
					'time': dt.isoformat(),
					'type': action,
					'price': float(order.executed.price),
					'size': float(order.executed.size),
				}
				self._trade_log.append(entry)
				_broadcast(outer_loop, outer_ws_manager, outer_run_id, { 'type': 'trade', 'trade': entry })
				# Remove from open orders list when completed
				try:
					self.orders.remove(order)  # type: ignore[attr-defined]
				except Exception:
					pass
			else:
				# Broadcast order lifecycle events to help debug rejections/margin
				dt = bt.num2date(self.datas[0].datetime[0])
				status_map = {
					order.Submitted: 'Submitted',
					order.Accepted: 'Accepted',
					order.Partial: 'Partial',
					order.Rejected: 'Rejected',
					order.Canceled: 'Canceled',
					order.Expired: 'Expired',
					order.Margin: 'Margin',
				}
				status_str = status_map.get(order.status, str(order.status))
				try:
					px = float(getattr(order.executed, 'price', 0.0) or 0.0)
					sz = float(getattr(order.executed, 'size', 0.0) or 0.0)
				except Exception:
					px, sz = 0.0, 0.0
				_broadcast(outer_loop, outer_ws_manager, outer_run_id, {
					'type': 'order',
					'time': dt.isoformat(),
					'status': status_str,
					'side': ('buy' if order.isbuy() else 'sell'),
					'price': px,
					'size': sz,
				})
				# Remove from open orders list on terminal states
				if status_str in ('Rejected', 'Canceled', 'Expired', 'Margin'):
					try:
						self.orders.remove(order)  # type: ignore[attr-defined]
					except Exception:
						pass

		def notify_trade(self, trade):
			if trade.isclosed:
				dt = bt.num2date(self.datas[0].datetime[0])
				pnl_val = float(getattr(trade, 'pnlcomm', trade.pnl))
				# If the last entry is a 'close' for this bar, enrich it with PnL instead of emitting a duplicate marker
				if self._trade_log and self._trade_log[-1].get('type') == 'close' and self._trade_log[-1].get('time') == dt.isoformat():
					self._trade_log[-1]['pnl'] = pnl_val
				else:
					self._trade_log.append({
						'time': dt.isoformat(),
						'type': 'close',
						'pnl': pnl_val,
						'size': float(trade.size),
						'price': float(self.datas[0].close[0]),
					})
				# Do not broadcast here to avoid duplicate close markers on the same bar

		def next(self):
			# Keep common broker aliases in sync for compatibility with translated strategies
			try:
				self.broker.equity = float(self.broker.getvalue())  # type: ignore[attr-defined]
				self.broker.cash = float(self.broker.getcash())     # type: ignore[attr-defined]
				self.broker.value = float(self.broker.getvalue())   # type: ignore[attr-defined]
			except Exception:
				pass
			# User logic
			try:
				super().next()
			except Exception as exc:  # noqa: BLE001
				# Broadcast the error before letting it bubble to stop the run
				_broadcast(outer_loop, outer_ws_manager, outer_run_id, {
					'type': 'error',
					'message': f'Strategy error: {exc}',
				})
				raise
			# Publish current bar for first data (single-asset)
			bar_time = bt.num2date(self.datas[0].datetime[0])
			name = self.datas[0]._name or 'data'
			o = float(self.datas[0].open[0])
			h = float(self.datas[0].high[0])
			l = float(self.datas[0].low[0])
			c = float(self.datas[0].close[0])
			_broadcast(outer_loop, outer_ws_manager, outer_run_id, {
				'type': 'bar',
				'time': bar_time.isoformat(),
				'prices': {name: c},
				'ohlc': {name: {'o': o, 'h': h, 'l': l, 'c': c}},
				'portfolio_value': float(self.broker.getvalue()),
			})
			# Blocking sleep to maintain consistent pacing for frontend rendering
			time.sleep(max(0, stream_delay_ms)/1000.0)

	# Add strategy
	cerebro.addstrategy(StrategyWrapper)

	_broadcast(loop, ws_manager, run_id, {"type": "status", "message": "starting"})

	# Run in executor
	results = await loop.run_in_executor(None, cerebro.run)

	final_value = float(cerebro.broker.getvalue())
	ana = results[0].analyzers if results else None
	metrics = {}
	if ana:
		def ga(name):
			try:
				return ana.__dict__[name].get_analysis()
			except Exception:
				return {}
		metrics['drawdown'] = ga('dd')
		metrics['sharpe'] = ga('sharpe')
		metrics['trades'] = ga('trades')
		metrics['returns'] = ga('returns')
		metrics['sqn'] = ga('sqn')

	# Summary metrics similar to example style
	try:
		pv_start = float(initial_cash)
		strategy_return_pct = ((final_value - pv_start) / pv_start) * 100.0 if pv_start else None
		# Compute annualized return (CAGR) based on backtest period
		annual_return_pct = None
		try:
			period_days = max(1.0, float((end - start).total_seconds()) / 86400.0)
			if pv_start and period_days > 0:
				annual_return_pct = ((final_value / pv_start) ** (365.0 / period_days) - 1.0) * 100.0
		except Exception:
			annual_return_pct = None
		buy_hold_return_pct = None
		buy_hold_max_dd_pct = None
		if first_close_series is not None and len(first_close_series) > 1:
			start_price = float(first_close_series.iloc[0])
			end_price = float(first_close_series.iloc[-1])
			if start_price:
				buy_hold_return_pct = ((end_price - start_price) / start_price) * 100.0
			rolling_max = first_close_series.expanding().max()
			drawdowns = (first_close_series - rolling_max) / rolling_max * 100.0
			buy_hold_max_dd_pct = float(abs(drawdowns.min()))

		tr = metrics.get('trades', {}) if isinstance(metrics, dict) else {}
		total_trades = (tr.get('total', {}) or {}).get('total')
		won_total = (tr.get('won', {}) or {}).get('total')
		lost_total = (tr.get('lost', {}) or {}).get('total')
		win_rate_pct = (won_total / total_trades * 100.0) if total_trades and won_total is not None else None
		avg_pnl_per_trade = (((tr.get('pnl', {}) or {}).get('net', {}) or {}).get('average'))
		largest_win = (((tr.get('won', {}) or {}).get('pnl', {}) or {}).get('max'))
		largest_loss = (((tr.get('lost', {}) or {}).get('pnl', {}) or {}).get('max'))
		longest_dd_bars = ((metrics.get('drawdown', {}) or {}).get('max', {}) or {}).get('len')
		# approximate longest drawdown in days from bars based on timeframe
		bars_per_day = 24 if timeframe == 'H1' else (96 if timeframe == 'M15' else 1)
		longest_dd_days = (float(longest_dd_bars) / float(bars_per_day)) if longest_dd_bars else None

		# Sharpe convenience
		sharpe_ratio = None
		try:
			sharpe_ratio = (metrics.get('sharpe', {}) or {}).get('sharperatio')
		except Exception:
			pass

		metrics['summary'] = {
			'pv_start': pv_start,
			'pv_end': final_value,
			'strategy_return_pct': strategy_return_pct,
			'annual_return_pct': annual_return_pct,
			'buy_hold_return_pct': (buy_hold_return_pct if include_buy_hold else None),
			'strategy_vs_buy_hold_pct': ((strategy_return_pct - buy_hold_return_pct) if (include_buy_hold and strategy_return_pct is not None and buy_hold_return_pct is not None) else None),
			'strategy_max_dd_pct': ((metrics.get('drawdown', {}) or {}).get('max', {}) or {}).get('drawdown'),
			'buy_hold_max_dd_pct': (buy_hold_max_dd_pct if include_buy_hold else None),
			'drawdown_diff_pct': ( (((metrics.get('drawdown', {}) or {}).get('max', {}) or {}).get('drawdown') - buy_hold_max_dd_pct) if (include_buy_hold and metrics.get('drawdown') and buy_hold_max_dd_pct is not None) else None ),
			'total_trades': total_trades,
			'won_trades': won_total,
			'lost_trades': lost_total,
			'win_rate_pct': win_rate_pct,
			'avg_pnl_per_trade': avg_pnl_per_trade,
			'largest_win': largest_win,
			'largest_loss': largest_loss,
			'longest_drawdown_bars': longest_dd_bars,
			'longest_drawdown_days': longest_dd_days,
			'sharpe_ratio': sharpe_ratio,
		}
	except Exception:
		pass
	# Sanitize payload before sending to the browser to avoid JSON.parse errors
	safe_metrics = _sanitize_json_numbers(metrics)
	safe_trades = _sanitize_json_numbers(trade_log)
	_broadcast(loop, ws_manager, run_id, {
		'type': 'done',
		'portfolio_value': final_value,
		'metrics': safe_metrics,
		'trades': safe_trades,
	})
