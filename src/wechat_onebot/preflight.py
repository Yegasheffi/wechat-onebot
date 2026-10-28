"""启动前检查与单实例管理。

把这次调试踩到的坑固化成代码：
  * 旧网关进程没退干净 → 端口被占 → 新进程"看似启动"，其实 WebUI/WS 全没起来
  * 启动前先探测所有要监听的端口，直接给出占用者，避免半启动状态
  * 用 PID 文件支持 `--stop` 干净地停掉上一实例
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .config import ConfigStore

PID_FILE_NAME = "gateway.pid"


@dataclass
class Binding:
    label: str
    host: str
    port: int


def _normalize_bind_host(host: str) -> tuple:
    host = (host or "0.0.0.0").strip()
    if host in ("0.0.0.0", "*", ""):
        return "0.0.0.0", socket.AF_INET
    if host in ("::", "[::]"):
        return "::", socket.AF_INET6
    return host, socket.AF_INET


def collect_bindings(store: ConfigStore) -> List[Binding]:
    bindings: List[Binding] = []
    network = (store.onebot11.get("network") or {})

    for cfg in network.get("websocketServers") or []:
        if cfg.get("enable", False):
            bindings.append(
                Binding(
                    f"正向 WS [{cfg.get('name') or cfg.get('port')}]",
                    str(cfg.get("host") or "0.0.0.0"),
                    int(cfg.get("port") or 3001),
                )
            )
    for cfg in network.get("httpServers") or []:
        if cfg.get("enable", False):
            bindings.append(
                Binding(
                    f"HTTP API [{cfg.get('name') or cfg.get('port')}]",
                    str(cfg.get("host") or "0.0.0.0"),
                    int(cfg.get("port") or 5700),
                )
            )

    callback = store.wechat.get("callback") or {}
    if callback.get("enable", True):
        bindings.append(
            Binding("驱动回调", str(callback.get("host") or "0.0.0.0"), int(callback.get("port") or 9002))
        )

    media = store.wechat.get("media") or {}
    if media.get("enable", True) and store.onebot11.get("enableLocalFile2Url", True):
        bindings.append(
            Binding("媒体服务", str(media.get("host") or "0.0.0.0"), int(media.get("port") or 3002))
        )

    webui = store.webui_config()
    if webui.get("enable", True):
        bindings.append(
            Binding("WebUI", str(webui.get("host") or "0.0.0.0"), int(webui.get("port") or 6100))
        )

    # 去重（例如把回调端口和 WebUI 配成同一个）
    unique: List[Binding] = []
    seen = set()
    for item in bindings:
        key = (item.host, item.port)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def can_bind(host: str, port: int) -> bool:
    bind_host, family = _normalize_bind_host(host)
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def port_owner(port: int) -> str:
    """尽量找出监听该端口的进程，便于提示用户。"""
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, PermissionError, subprocess.SubprocessError):
        return ""
    lines = [line for line in (result.stdout or "").splitlines()[1:] if line.strip()]
    if not lines:
        return ""
    parts = lines[0].split()
    if len(parts) >= 2:
        return f"{parts[0]} (pid {parts[1]})"
    return ""


def check(store: ConfigStore) -> List[str]:
    problems: List[str] = []
    for binding in collect_bindings(store):
        if can_bind(binding.host, binding.port):
            continue
        owner = port_owner(binding.port)
        suffix = f"（{owner}）" if owner else ""
        problems.append(f"{binding.label} 端口 {binding.port} 已被占用{suffix}")
    return problems


def warnings(store: ConfigStore) -> List[str]:
    """非致命提示，例如协议服务没起（仍然允许启动，WebUI 里可以看到错误）。"""
    items: List[str] = []
    if store.driver_name == "ipad":
        base = str((store.wechat.get("ipad") or {}).get("base_url") or "")
        try:
            parsed = urllib.parse.urlsplit(base)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=1.5):
                pass
        except Exception:
            items.append(
                f"协议服务 {base or '（未配置 base_url）'} 不可连接；扫码登录会失败，"
                "请检查 docker logs wechat-ipad-v311"
            )
    return items


# ---------------------------------------------------------------------------
# PID 文件 / 停止实例
# ---------------------------------------------------------------------------


def run_dir(config_dir: Path) -> Path:
    return config_dir.parent / "run"


def pid_path(config_dir: Path) -> Path:
    return run_dir(config_dir) / PID_FILE_NAME


def write_pid(config_dir: Path) -> Path:
    path = pid_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def remove_pid(config_dir: Path) -> None:
    try:
        pid_path(config_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_instance(store: ConfigStore) -> int:
    path = pid_path(store.dir)
    if path.exists():
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = 0
        if pid and _alive(pid):
            print(f"正在停止 wechat-onebot（pid {pid}）…")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc:
                print(f"发送停止信号失败：{exc}")
                return 1
            for _ in range(50):
                time.sleep(0.2)
                if not _alive(pid):
                    remove_pid(store.dir)
                    print("已停止")
                    return 0
            print("进程未在 10 秒内退出，强制结束")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            remove_pid(store.dir)
            return 0
        remove_pid(store.dir)

    # 没有 PID 文件：至少把占用端口的进程列出来
    busy = []
    for binding in collect_bindings(store):
        if can_bind(binding.host, binding.port):
            continue
        owner = port_owner(binding.port) or "未知进程"
        busy.append(f"  · {binding.label} 端口 {binding.port}：{owner}")
    if busy:
        print("没有找到本程序的 PID 文件，但以下端口仍被占用：")
        print("\n".join(busy))
        print("请手动结束上面的进程，或修改对应端口后重启。")
        return 1
    print("没有正在运行的 wechat-onebot 实例。")
    return 0
