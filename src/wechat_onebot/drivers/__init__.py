"""驱动注册表。"""

from __future__ import annotations

import importlib
from typing import Any, Dict

from ..model import UnsupportedAction

_DRIVERS: Dict[str, tuple] = {
    "mock": ("mock", "MockDriver"),
    "ipad": ("ipad", "IpadDriver"),
    "bridge": ("bridge", "BridgeDriver"),
}

DRIVER_NAMES = sorted(_DRIVERS)


def create_driver(name: str, config: Dict[str, Any], ctx: Any):
    key = (name or "mock").strip().lower()
    if key not in _DRIVERS:
        raise UnsupportedAction(f"未知驱动 {name}，可选：{', '.join(DRIVER_NAMES)}")
    module_name, class_name = _DRIVERS[key]
    module = importlib.import_module(f".{module_name}", __package__)
    driver_cls = getattr(module, class_name)
    return driver_cls(config.get(key, config) if isinstance(config, dict) else {}, ctx)
