"""配置加载：仿 NapCat 的三份配置。

    config/onebot11.json  —— OneBot 11 网络配置（与 NapCat 的 onebot11_<账号>.json 结构对齐）
    config/wechat.json    —— 微信侧驱动配置（驱动选择、账号、iPad/webhook 参数、回调端口）
    config/webui.json     —— WebUI 监听地址与登录令牌

首次运行会自动生成默认文件；所有写入都是原子写（tmp + rename）。
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict

from . import APP_NAME, __version__

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_ONEBOT11: Dict[str, Any] = {
    "network": {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [
            {
                "enable": True,
                "name": "onebot-ws",
                "host": "0.0.0.0",
                "port": 3001,
                "path": "/onebot",
                "token": "",
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "debug": False,
                "heartInterval": 30000,
            }
        ],
        "websocketClients": [
            {
                "enable": False,
                "name": "onebot-ws-reverse",
                "url": "ws://127.0.0.1:5140/onebot",
                "token": "",
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "debug": False,
                "heartInterval": 30000,
                "reconnectInterval": 3000,
            }
        ],
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": True,
    "parseMultMsg": False,
    "imageDownloadProxy": "",
    "timeout": {
        "baseTimeout": 10000,
        "uploadSpeedKBps": 256,
        "downloadSpeedKBps": 256,
        "maxTimeout": 1800000,
    },
}

DEFAULT_WECHAT: Dict[str, Any] = {
    "driver": "mock",
    "account": {"wxid": "", "nickname": ""},
    "callback": {"enable": True, "host": "0.0.0.0", "port": 9002, "token": ""},
    "mock": {
        "interactive": True,
        "friends": [
            {"user_id": "wxid_xiaoai", "nickname": "小爱", "remark": "测试好友"},
            {"user_id": "wxid_abobo", "nickname": "阿波", "remark": ""},
        ],
        "groups": [
            {
                "group_id": "12345678@chatroom",
                "name": "微信测试群",
                "owner": "wxid_xiaoai",
                "admins": ["wxid_mock_bot"],
                "members": [
                    {"user_id": "wxid_xiaoai", "nickname": "小爱"},
                    {"user_id": "wxid_abobo", "nickname": "阿波"},
                ],
            }
        ],
    },
    "media": {"enable": True, "host": "0.0.0.0", "port": 3002, "public_base": ""},
    "ipad": {
        "base_url": "http://127.0.0.1:3010",
        "api_prefix": "/api",
        "wxid": "",
        "timeout": 20,
        "login_mode": "ipad",
        "callback_path_prefix": "/api/v1/wechat-client",
        "refresh_contacts_sec": 300,
        "login": {
            "auto_login": True,
            "device_name": "wechat-onebot",
            "login_type": "ipad",
            "heartbeat_sec": 25,
        },
    },
    "bridge": {
        "account": {"wxid": "bridge_account", "nickname": "微信桥"},
        "action_callback_url": "",
        "action_timeout": 30,
        "token": "",
    },
}


def _default_webui() -> Dict[str, Any]:
    return {
        "enable": True,
        "host": "0.0.0.0",
        "port": 6100,
        "token": secrets.token_urlsafe(9),
    }


def project_root() -> Path:
    """项目根目录（src/wechat_onebot/config.py -> 上三级）。"""
    return Path(__file__).resolve().parents[2]


def resolve_config_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("WECHAT_ONEBOT_HOME")
    if env:
        return Path(env).expanduser().resolve() / "config"
    return project_root() / "config"


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")
    return data


class ConfigStore:
    """三份配置的统一读写入口。"""

    def __init__(self, config_dir: Path) -> None:
        self.dir = config_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.paths = {
            "onebot11": self.dir / "onebot11.json",
            "wechat": self.dir / "wechat.json",
            "webui": self.dir / "webui.json",
        }
        self.onebot11: Dict[str, Any] = {}
        self.wechat: Dict[str, Any] = {}
        self.webui: Dict[str, Any] = {}
        self._raw: Dict[str, Dict[str, Any]] = {}
        self.load()

    # -- 加载 --------------------------------------------------------------
    def load(self) -> None:
        defaults = {
            "onebot11": DEFAULT_ONEBOT11,
            "wechat": DEFAULT_WECHAT,
            "webui": _default_webui(),
        }
        for name, path in self.paths.items():
            if path.exists():
                raw = _read_json(path)
            else:
                raw = copy.deepcopy(defaults[name])
                if name == "webui":
                    raw = _default_webui()
                _atomic_write(path, raw)
            self._raw[name] = raw
            merged = _deep_merge(defaults[name], raw)
            setattr(self, name, merged)

    # -- 保存 --------------------------------------------------------------
    def save(self, name: str) -> None:
        assert name in self.paths
        _atomic_write(self.paths[name], self._raw[name])

    def raw(self, name: str) -> Dict[str, Any]:
        return copy.deepcopy(self._raw[name])

    def update_raw(self, name: str, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        _atomic_write(self.paths[name], data)
        self._raw[name] = copy.deepcopy(data)

    # -- 便捷读取 ----------------------------------------------------------
    @property
    def driver_name(self) -> str:
        return str(self.wechat.get("driver") or "mock").strip().lower()

    def account(self) -> Dict[str, str]:
        acc = self.wechat.get("account") or {}
        return {
            "wxid": str(acc.get("wxid") or ""),
            "nickname": str(acc.get("nickname") or ""),
        }

    def webui_config(self) -> Dict[str, Any]:
        cfg = dict(self.webui)
        cfg.setdefault("enable", True)
        cfg.setdefault("host", "0.0.0.0")
        cfg.setdefault("port", 6100)
        cfg.setdefault("token", "")
        return cfg

    def summary(self) -> Dict[str, Any]:
        return {
            "app": APP_NAME,
            "version": __version__,
            "config_dir": str(self.dir),
            "driver": self.driver_name,
        }


def parse_simple_args(argv: list[str]) -> Dict[str, str]:
    """极简参数解析：--flag value / --flag=value。"""
    out: Dict[str, str] = {}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.startswith("--"):
            key = item[2:]
            if "=" in key:
                key, value = key.split("=", 1)
                out[key] = value
            elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                out[key] = argv[index + 1]
                index += 1
            else:
                out[key] = "true"
        index += 1
    return out
