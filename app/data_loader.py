from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Tuple

import pandas as pd

try:
	import MetaTrader5 as mt5
except Exception:  # noqa: BLE001
	mt5 = None  # Optional

import yfinance as yf


YF_TIMEFRAME_MAP = {
	"M1": "1m",
	"M5": "5m",
	"M15": "15m",
	"M30": "30m",
	"H1": "1h",
	"H4": "4h",
	"D1": "1d",
}


def _ensure_naive_utc_index(df: pd.DataFrame) -> pd.DataFrame:
	if df.index.tz is not None:
		df = df.tz_convert("UTC").tz_localize(None)
	return df


def load_yfinance(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
	interval = YF_TIMEFRAME_MAP.get(timeframe)
	if not interval:
		raise ValueError(f"Unsupported timeframe for yfinance: {timeframe}")

	# Clamp intraday ranges to provider limits (~730 days)
	if any(k in interval for k in ("m","h")):
		max_days = 730
		if end and start and (end - start).days > max_days:
			start = end - timedelta(days=max_days)

	df = yf.download(
		tickers=symbol,
		start=start,
		end=end,
		interval=interval,
		auto_adjust=False,
		progress=False,
		prepost=False,
		threads=True,
	)

	if df.empty:
		raise ValueError(f"No data returned from yfinance for {symbol}")

	# yfinance returns multi-index columns for multiple tickers; ensure flat OHLCV
	if isinstance(df.columns, pd.MultiIndex):
		# Take the first level for the symbol
		df = df.xs(symbol, axis=1, level=1)

	df = df.rename(columns={
		"Open": "open",
		"High": "high",
		"Low": "low",
		"Close": "close",
		"Adj Close": "adj_close",
		"Volume": "volume",
	})

	df = _ensure_naive_utc_index(df)
	df = df[[c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]].copy()
	df.dropna(inplace=True)
	return df


MT5_TIMEFRAME_MAP: Dict[str, int] = {}
if mt5 is not None:
	MT5_TIMEFRAME_MAP = {
		"M1": mt5.TIMEFRAME_M1,
		"M5": mt5.TIMEFRAME_M5,
		"M15": mt5.TIMEFRAME_M15,
		"M30": mt5.TIMEFRAME_M30,
		"H1": mt5.TIMEFRAME_H1,
		"H4": mt5.TIMEFRAME_H4,
		"D1": mt5.TIMEFRAME_D1,
	}


def load_mt5(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
	if mt5 is None:
		raise RuntimeError("MetaTrader5 module not available")

	tf = MT5_TIMEFRAME_MAP.get(timeframe)
	if tf is None:
		raise ValueError(f"Unsupported timeframe for MT5: {timeframe}")

	if not mt5.initialize():
		raise RuntimeError("Failed to initialize MT5 terminal. Is it installed and logged in?")

	try:
		rates = mt5.copy_rates_range(symbol, tf, start, end)
		if rates is None or len(rates) == 0:
			raise ValueError(f"No MT5 data for {symbol}")
		df = pd.DataFrame(rates)
		df["time"] = pd.to_datetime(df["time"], unit="s")
		df.set_index("time", inplace=True)
		df.rename(columns={
			"open": "open",
			"high": "high",
			"low": "low",
			"close": "close",
			"tick_volume": "volume",
		}, inplace=True)
		df = df[["open", "high", "low", "close", "volume"]]
		return df
	finally:
		mt5.shutdown()


def timeframe_to_bt(timeframe: str) -> Tuple[int, int]:
	import backtrader as bt

	if timeframe == "H1":
		return bt.TimeFrame.Minutes, 60
	if timeframe == "M15":
		return bt.TimeFrame.Minutes, 15
	if timeframe == "D1":
		return bt.TimeFrame.Days, 1
	# Default fallback
	return bt.TimeFrame.Minutes, 60
