"""Lightweight stdlib-based logger."""

from __future__ import annotations

import logging
import os
from typing import Any

_DEFAULT_LEVEL = logging.INFO
_ENV_LEVEL = "PR_TEST_AUTOMATOR_LOG_LEVEL"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s : %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def _configure_once() -> None:
    global _configured
    if _configured:
        return

    level_name = os.environ.get(_ENV_LEVEL, "").upper()
    level = (
        getattr(logging, level_name, _DEFAULT_LEVEL)
        if level_name
        else _DEFAULT_LEVEL
    )

    root = logging.getLogger("pr_test_automator_local")
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(handler)
    root.propagate = False
    _configured = True


class _ContextAdapter(logging.LoggerAdapter):
    def process(
        self, msg: str, kwargs: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        extra: dict[str, Any] = kwargs.pop("extra", {}) or {}
        bound = {**(self.extra or {}), **extra}
        if bound:
            tail = " ".join(f"{k}={v}" for k, v in bound.items())
            msg = f"{msg} | {tail}"
        return msg, kwargs


def get_logger(name: str) -> logging.LoggerAdapter:
    _configure_once()
    return _ContextAdapter(logging.getLogger(name), {})
