from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Type

import backtrader as bt
import pandas as pd

from .data_loader import load_yfinance, load_mt5
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


def _load_df(symbol: str, source: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
	if source == "yfinance":
		return load_yfinance(symbol, timeframe, start, end)
	if source == "mt5":
		return load_mt5(symbol, timeframe, start, end)
	raise ValueError(f"Unsupported data source: {source}")


def _broadcast(loop: asyncio.AbstractEventLoop, ws_manager: WebSocketManager, run_id: str, message: Dict[str, Any]) -> None:
	asyncio.run_coroutine_threadsafe(ws_manager.broadcast(run_id, message), loop)


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
	stream_delay_ms: int,
	strategy_cls: Type[bt.Strategy],
) -> None:
	cerebro = bt.Cerebro()
	cerebro.broker.setcash(initial_cash)
	cerebro.broker.setcommission(commission=commission)
	# Apply percent slippage approximation
	if slippage and slippage > 0:
		cerebro.broker.set_slippage_perc(slippage)

	# Use a percent-of-cash sizer so position sizes meaningfully affect PnL
	try:
		cerebro.addsizer(bt.sizers.PercentSizer, percents=int(size_percent))
	except Exception:
		pass

	# Load data
	for symbol in symbols:
		df = await loop.run_in_executor(None, _load_df, symbol, source, timeframe, start, end)
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

		def notify_order(self, order):
			if order.status == order.Completed:
				dt = bt.num2date(self.datas[0].datetime[0])
				self._trade_log.append({
					'time': dt.isoformat(),
					'type': 'buy' if order.isbuy() else 'sell',
					'price': float(order.executed.price),
					'size': float(order.executed.size),
				})
				_broadcast(outer_loop, outer_ws_manager, outer_run_id, { 'type': 'trade', 'trade': self._trade_log[-1] })

		def notify_trade(self, trade):
			if trade.isclosed:
				dt = bt.num2date(self.datas[0].datetime[0])
				self._trade_log.append({
					'time': dt.isoformat(),
					'type': 'close',
					'pnl': float(getattr(trade, 'pnlcomm', trade.pnl)),
					'size': float(trade.size),
					'price': float(self.datas[0].close[0]),
				})
				_broadcast(outer_loop, outer_ws_manager, outer_run_id, { 'type': 'trade', 'trade': self._trade_log[-1] })

		async def _sleep(self):
			await asyncio.sleep(max(0, stream_delay_ms)/1000.0)

		def next(self):
			# User logic
			super().next()
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
			# throttle between bars for visualization
			time.sleep(max(0, stream_delay_ms)/1000.0)
			# throttle
			asyncio.run_coroutine_threadsafe(asyncio.sleep(max(0, stream_delay_ms)/1000.0), outer_loop)

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
	_broadcast(loop, ws_manager, run_id, {
		'type': 'done',
		'portfolio_value': final_value,
		'metrics': metrics,
		'trades': trade_log,
	})
