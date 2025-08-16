from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Tuple
import os
import math
import requests

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


def _estimate_bars(timeframe: str, start: datetime, end: datetime) -> int:
	seconds = max(0, int((end - start).total_seconds()))
	if timeframe == "M1":
		step = 60
	elif timeframe == "M5":
		step = 300
	elif timeframe == "M15":
		step = 900
	elif timeframe == "M30":
		step = 1800
	elif timeframe == "H1":
		step = 3600
	elif timeframe == "H4":
		step = 14400
	else:
		step = 86400
	return max(1, math.ceil(seconds / step))


OANDA_GRANULARITY_MAP: Dict[str, str] = {
	"M1": "M1",
	"M5": "M5",
	"M15": "M15",
	"M30": "M30",
	"H1": "H1",
	"H4": "H4",
	"D1": "D",
}


def load_oanda(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
	"""Load candles from Oanda v20 REST API.

	Requires env vars:
	- OANDA_API_KEY
	- OANDA_ENV = practice|live (default practice)

	Symbol should be Oanda instrument format, e.g., EUR_USD, XAU_USD.
	"""
	api_key = os.getenv("OANDA_API_KEY")
	if not api_key:
		raise RuntimeError("OANDA_API_KEY not set in environment")
	env = (os.getenv("OANDA_ENV") or "practice").lower()
	base = "https://api-fxpractice.oanda.com" if env != "live" else "https://api-fxtrade.oanda.com"
	gran = OANDA_GRANULARITY_MAP.get(timeframe)
	if not gran:
		raise ValueError(f"Unsupported timeframe for Oanda: {timeframe}")

	headers = {"Authorization": f"Bearer {api_key}"}
	# Oanda caps results per call (typically 5000). We'll use count=min(estimated, 5000) anchored at 'end'.
	est = _estimate_bars(timeframe, start, end)
	count = min(est, 5000)
	params = {
		"granularity": gran,
		"count": count,
		"price": "M",  # midpoint
		"to": end.replace(microsecond=0).isoformat() + "Z",
	}
	url = f"{base}/v3/instruments/{symbol}/candles"
	r = requests.get(url, headers=headers, params=params, timeout=30)
	if r.status_code != 200:
		raise RuntimeError(f"Oanda error {r.status_code}: {r.text[:200]}")
	data = r.json()
	candles = data.get("candles") or []
	if not candles:
		raise ValueError(f"No Oanda data for {symbol}")
	rows = []
	for c in candles:
		if not c.get("complete"):
			continue
		ts = pd.to_datetime(c["time"])  # RFC3339Z
		mid = c.get("mid") or {}
		rows.append({
			"time": ts,
			"open": float(mid.get("o")),
			"high": float(mid.get("h")),
			"low": float(mid.get("l")),
			"close": float(mid.get("c")),
			"volume": float(c.get("volume") or 0.0),
		})
	df = pd.DataFrame(rows)
	if df.empty:
		raise ValueError(f"No Oanda data for {symbol}")
	df.set_index("time", inplace=True)
	df = _ensure_naive_utc_index(df)
	return df


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
