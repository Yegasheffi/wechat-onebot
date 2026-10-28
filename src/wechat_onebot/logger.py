"""日志：控制台彩屏输出 + 内存环形缓冲（供 WebUI 的 SSE 实时日志使用）。"""

from __future__ import annotations

import collections
import logging
import sys
import threading
import time
from typing import Callable, Deque, Optional

LEVEL_COLORS = {
    "DEBUG": "\033[38;5;244m",
    "INFO": "\033[38;5;39m",
    "WARN": "\033[38;5;214m",
    "ERROR": "\033[38;5;196m",
    "RESET": "\033[0m",
}


class RingBufferHandler(logging.Handler):
    """把日志塞进环形缓冲，并分发给订阅者（WebUI SSE）。"""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.records: Deque[dict] = collections.deque(maxlen=capacity)
        self._subscribers: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {
                "time": int(record.created * 1000),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                item["message"] += "\n" + logging.Formatter().formatException(record.exc_info)
        except Exception:  # pragma: no cover - 日志自身不能抛错
            return
        with self._lock:
            self.records.append(item)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(item)
            except Exception:
                pass

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def snapshot(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return list(self.records)[-limit:]


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        color = LEVEL_COLORS.get(level, "")
        reset = LEVEL_COLORS["RESET"] if color else ""
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        name = record.name.replace("wechat_onebot.", "")
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        return f"{LEVEL_COLORS['DEBUG']}{stamp}{reset} {color}{level:<5}{reset} {name:<12} {message}"


ring_handler = RingBufferHandler()
_configured = False


def setup(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("wechat_onebot")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)
    root.addHandler(ring_handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger("wechat_onebot." + name)


def set_level(level: str) -> None:
    logging.getLogger("wechat_onebot").setLevel(getattr(logging, level.upper(), logging.INFO))


def subscribe_logs(callback: Callable[[dict], None]) -> Callable[[], None]:
    return ring_handler.subscribe(callback)


def recent_logs(limit: int = 200) -> list[dict]:
    return ring_handler.snapshot(limit)
