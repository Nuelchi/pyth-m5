from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional, Type

import backtrader as bt


STRATEGIES_DIR = Path(__file__).resolve().parent.parent / 'strategies'
STRATEGIES_DIR.mkdir(exist_ok=True)


def _find_strategy_class(module: ModuleType) -> Type[bt.Strategy]:
	for _, obj in inspect.getmembers(module, inspect.isclass):
		if issubclass(obj, bt.Strategy) and obj is not bt.Strategy:
			return obj
	raise ValueError('No bt.Strategy subclass found in provided code')


def load_strategy(*, code: Optional[str], name: Optional[str]) -> Type[bt.Strategy]:
	if code:
		module = ModuleType('user_strategy')
		# Provide backtrader symbol for convenience in user code
		module.__dict__['bt'] = bt
		exec(code, module.__dict__)
		return _find_strategy_class(module)
	if name:
		path = STRATEGIES_DIR / f"{name}.py"
		if not path.exists():
			raise FileNotFoundError(f"Strategy file not found: {path}")
		spec = importlib.util.spec_from_file_location(name, path)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		sys.modules[name] = module
		spec.loader.exec_module(module)  # type: ignore[attr-defined]
		return _find_strategy_class(module)
	# Default: example strategy
	default_path = STRATEGIES_DIR / 'example.py'
	if default_path.exists():
		spec = importlib.util.spec_from_file_location('example', default_path)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		sys.modules['example'] = module
		spec.loader.exec_module(module)  # type: ignore[attr-defined]
		return _find_strategy_class(module)
	raise ValueError('No strategy provided and example not found')
