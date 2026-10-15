from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar

import tradingagents.default_config as default_config

# Use default config but allow it to be overridden
_config: dict | None = None
_run_config = ContextVar("tradingagents_run_config", default=None)


@contextmanager
def run_config(config, ticker=None, trade_date=None):
    scoped = deepcopy(default_config.DEFAULT_CONFIG)
    for key, value in deepcopy(config).items():
        if isinstance(value, dict) and isinstance(scoped.get(key), dict):
            scoped[key].update(value)
        else:
            scoped[key] = value
    scoped["analysis_ticker"] = ticker
    scoped["analysis_date"] = trade_date
    token = _run_config.set(scoped)
    try:
        yield
    finally:
        _run_config.reset(token)


def initialize_config():
    """Initialize the configuration with default values."""
    global _config
    if _config is None:
        _config = deepcopy(default_config.DEFAULT_CONFIG)


def set_config(config: dict):
    """Update the configuration with custom values.

    Dict-valued keys (e.g. ``data_vendors``) are merged one level deep so a
    partial update like ``{"data_vendors": {"core_stock_apis": "alpha_vantage"}}``
    keeps the other nested keys from the default; scalar keys are replaced.
    """
    global _config
    initialize_config()
    incoming = deepcopy(config)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(_config.get(key), dict):
            _config[key].update(value)
        else:
            _config[key] = value


def get_config() -> dict:
    """Get the current configuration."""
    if _run_config.get() is not None:
        return deepcopy(_run_config.get())
    if _config is None:
        initialize_config()
    return deepcopy(_config)


# Initialize with default config
initialize_config()
