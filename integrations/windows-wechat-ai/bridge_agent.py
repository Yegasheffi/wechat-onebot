#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat-onebot 的 Windows 端桥接代理。

运行在**真实的 Windows 10/11** 上，连接本机已登录的微信 4.x 客户端：

    微信 4.1.15.x  ──UIA/数据库──>  本脚本  ──HTTP──>  wechat-onebot 的 bridge 驱动

- 收消息：wechatauto 监听新消息 → POST /wechat/event
- 发消息：GET /wechat/actions 取任务 → 微信发送 → POST /wechat/actions/result

不依赖 WeChatFerry，不做 DLL 注入；走的是微信 4.x 官方客户端 + UIA 自动化，
所以不会被“客户端版本过低”拦截。

用法：
    python bridge_agent.py --config config.json
    python bridge_agent.py --doctor
    python bridge_agent.py --mock          # 不启动微信，用假数据自检桥接协议
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

LOG = logging.getLogger("wechat-bridge")

DEFAULTS = {
    "gateway": "http://127.0.0.1:9002",
    "token": "",
    "poll_wait": 25,
    "request_timeout": 15,
    "log_file": "",
    "account_nickname": "微信 AI",
}

GROUP_PREFIX = re.compile(r"^(wxid_[0-9a-zA-Z_]+):\s*\n")


# ---------------------------------------------------------------------------
# HTTP 小工具（只用标准库，Windows 上无需 requests）
# ---------------------------------------------------------------------------


def http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    token: str = "",
    timeout: float = 15,
) -> Dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Token"] = token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# 微信适配层
# ---------------------------------------------------------------------------


class RealWeChat:
    """真实微信 4.x：wechatauto-replica（UIA + 本地数据库）。"""

    def __init__(self) -> None:
        from wechatauto import WeChat  # 延迟导入，便于 macOS 上跑 mock 测试

        self.wx = WeChat()
        self._stop = threading.Event()

    def self_info(self) -> tuple[str, str]:
        nickname = getattr(self.wx, "nickname", "") or ""
        wxid = ""
        try:
            wxid = str(self.wx._db.get_self_info().get("username") or "")
        except Exception:
            try:
                wxid = str(self.wx._db.wxid or "")
            except Exception:
                wxid = ""
        return wxid, nickname

    def start(self, on_message: Callable[[Any, Any], None]) -> None:
        self.wx.AddListenAll(on_message)
        self.wx.StartListening()

    def run(self) -> None:
        self.wx.KeepRunning()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.wx.StopListening(True)
        except Exception:
            pass

    def send_text(self, chat_id: str, text: str) -> bool:
        # 微信 GUI 的搜索框不认裸 wxid，先把 wxid 解析成昵称/群名再发
        target = chat_id
        try:
            name = self.wx._db.get_nickname(chat_id)
            if name:
                target = name
        except Exception:
            pass
        result = self.wx.SendMsg(text, who=target)
        return bool(result)


class MockWeChat:
    """不启动微信的假实现，用于在 macOS/CI 上验证桥接协议。"""

    def __init__(self) -> None:
        self.self_id = "wxid_mock_bot"
        self.nickname = "测试机器人"
        self.sent: list[tuple[str, str]] = []
        self._callback: Optional[Callable[[Any, Any], None]] = None
        self._stop = threading.Event()
        self._counter = 0

    def self_info(self) -> tuple[str, str]:
        return self.self_id, self.nickname

    def start(self, on_message: Callable[[Any, Any], None]) -> None:
        self._callback = on_message

    def run(self) -> None:
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()

    def send_text(self, chat_id: str, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True

    def emit(
        self,
        chat_id: str,
        text: str,
        sender_id: str = "wxid_alice",
        sender_name: str = "小爱",
        local_id: Optional[int] = None,
    ) -> None:
        if self._callback is None:
            raise RuntimeError("MockWeChat 还没 start")
        self._counter += 1
        message = type(
            "MockMessage",
            (),
            {
                "content": text,
                "sender_wxid": sender_id,
                "sender": sender_name,
                "attr": "friend",
                "local_id": local_id or self._counter,
                "id": str(local_id or self._counter),
                "type": "文本",
                "create_time": int(time.time()),
            },
        )()
        chat = type("MockChat", (), {"who": chat_id, "nickname": chat_id})()
        self._callback(message, chat)


# ---------------------------------------------------------------------------
# 桥接代理
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    gateway: str
    token: str = ""
    poll_wait: int = 25
    request_timeout: float = 15
    account_nickname: str = "微信 AI"
    log_file: str = ""


class BridgeAgent:
    def __init__(self, config: AgentConfig, wechat: Any) -> None:
        self.config = config
        self.wechat = wechat
        self.self_id = ""
        self.nickname = config.account_nickname
        self.base = config.gateway.rstrip("/")
        self.stop_event = threading.Event()
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=500)
        self.seen: set[tuple[str, str]] = set()
        self._seen_order: list[tuple[str, str]] = []
        self._event_thread: Optional[threading.Thread] = None
        self._action_thread: Optional[threading.Thread] = None

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> None:
        wxid, nickname = self.wechat.self_info()
        self.self_id = wxid
        if nickname:
            self.nickname = nickname

        self.wechat.start(self.on_message)
        self._event_thread = threading.Thread(target=self._event_loop, name="event-loop", daemon=True)
        self._action_thread = threading.Thread(target=self._action_loop, name="action-loop", daemon=True)
        self._event_thread.start()
        self._action_thread.start()

        if self.self_id:
            try:
                self.post_event(
                    {
                        "type": "account",
                        "user_id": self.self_id,
                        "nickname": self.nickname,
                    }
                )
            except Exception as exc:
                LOG.warning("上报账号失败（网关稍后会自动重试）：%s", exc)
        LOG.info("已连接微信：%s (%s)", self.nickname, self.self_id or "未知 wxid")
        LOG.info("网关：%s", self.base)

    def run(self) -> None:
        try:
            self.wechat.run()
        finally:
            self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.wechat.stop()
        except Exception:
            pass

    # -- 收消息 ------------------------------------------------------------
    def on_message(self, message: Any, chat: Any) -> None:
        try:
            chat_id = str(getattr(chat, "who", "") or "")
            if not chat_id:
                return
            if getattr(message, "attr", "") == "self":
                return
            sender_id = str(getattr(message, "sender_wxid", "") or "")
            if self.self_id and sender_id == self.self_id:
                return

            text = str(getattr(message, "content", "") or "")
            if chat_id.endswith("@chatroom"):
                match = GROUP_PREFIX.match(text)
                if match:
                    sender_id = sender_id or match.group(1)
                    text = text[match.end() :]
            text = text.strip()
            if not text:
                return

            local_id = str(getattr(message, "local_id", "") or getattr(message, "id", "") or "")
            key = (chat_id, local_id)
            if local_id and key in self.seen:
                return  # 监听层偶发重复投递
            if local_id:
                self._remember(key)

            chat_type = "group" if chat_id.endswith("@chatroom") else "private"
            sender_name = str(getattr(message, "sender", "") or sender_id or chat_id)
            event = {
                "type": "message",
                "chat_type": chat_type,
                "chat_id": chat_id,
                "platform_id": local_id or f"wechat-{int(time.time() * 1000)}",
                "sender": {
                    "user_id": sender_id or chat_id,
                    "nickname": sender_name,
                    "role": "member",
                },
                "segments": [{"type": "text", "data": {"text": text}}],
                "time": int(getattr(message, "create_time", 0) or time.time()),
            }
            if not self.events.full():
                self.events.put_nowait(event)
        except Exception:
            LOG.exception("处理微信消息失败")

    def _remember(self, key: tuple[str, str]) -> None:
        self.seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 5000:
            old = self._seen_order.pop(0)
            self.seen.discard(old)

    def _event_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.post_event(event)
            except Exception as exc:
                LOG.warning("上报消息失败：%s", exc)

    def post_event(self, event: Dict[str, Any]) -> None:
        http_json(
            "POST",
            f"{self.base}/wechat/event",
            event,
            self.config.token,
            self.config.request_timeout,
        )

    # -- 发消息 ------------------------------------------------------------
    def _action_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                data = http_json(
                    "GET",
                    f"{self.base}/wechat/actions?wait={int(self.config.poll_wait)}",
                    None,
                    self.config.token,
                    self.config.request_timeout + self.config.poll_wait,
                )
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                LOG.warning("拉取发送任务失败：%s", exc)
                time.sleep(3)
                continue

            for task in data.get("tasks") or []:
                self._handle_task(task)

    def _handle_task(self, task: Dict[str, Any]) -> None:
        task_id = task.get("id")
        ok = False
        platform_id = ""
        try:
            action = task.get("action")
            if action != "send_message":
                raise ValueError(f"不支持的 action: {action}")
            chat_id = str(task.get("chat_id") or "")
            text = segments_to_text(task.get("segments") or [])
            if not chat_id or not text:
                raise ValueError("任务缺少 chat_id 或文本内容")
            ok = bool(self.wechat.send_text(chat_id, text))
            if not ok:
                raise RuntimeError("微信发送失败")
            platform_id = f"win-{int(time.time() * 1000)}"
            LOG.info("已发送 -> %s：%s", chat_id, text[:50])
        except Exception as exc:
            LOG.warning("处理发送任务失败：%s", exc)
            ok = False
        finally:
            try:
                http_json(
                    "POST",
                    f"{self.base}/wechat/actions/result",
                    {"id": task_id, "ok": ok, "platform_id": platform_id},
                    self.config.token,
                    self.config.request_timeout,
                )
            except Exception as exc:
                if not self.stop_event.is_set():
                    LOG.warning("回传任务结果失败：%s", exc)


def segments_to_text(segments: list[Dict[str, Any]]) -> str:
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            parts.append("@" + str(data.get("name") or data.get("qq") or ""))
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "face":
            parts.append("[表情]")
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load_config(path: str) -> AgentConfig:
    values = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            values.update(json.load(handle) or {})
    values["gateway"] = os.environ.get("WECHAT_BRIDGE_GATEWAY", values["gateway"])
    values["token"] = os.environ.get("WECHAT_BRIDGE_TOKEN", values["token"])
    return AgentConfig(
        gateway=str(values["gateway"]),
        token=str(values.get("token") or ""),
        poll_wait=int(values.get("poll_wait") or 25),
        request_timeout=float(values.get("request_timeout") or 15),
        account_nickname=str(values.get("account_nickname") or "微信 AI"),
        log_file=str(values.get("log_file") or ""),
    )


def setup_logging(config: AgentConfig, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.log_file:
        handlers.append(logging.FileHandler(config.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def doctor(config: AgentConfig) -> int:
    print("== wechat-onebot Windows 代理自检 ==")
    ok = True
    try:
        import wechatauto

        print(f"✅ wechatauto-replica {getattr(wechatauto, '__version__', '?')}")
    except Exception as exc:
        print(f"❌ 无法导入 wechatauto-replica：{exc}")
        print("   在 Windows 上执行：pip install -r requirements.txt")
        return 1

    try:
        from wechatauto import WeChat

        wx = WeChat()
        print(f"✅ 微信已连接：{wx.nickname}")
        try:
            info = wx._db.get_self_info()
            print(f"   账号：{info.get('nick_name') or ''} ({info.get('username') or '?'})")
        except Exception as exc:
            print(f"⚠️  读取账号数据库失败：{exc}")
        try:
            wx.StopListening(False)
        except Exception:
            pass
    except Exception as exc:
        print(f"❌ 无法连接微信 4.x：{exc}")
        print("   确认微信已登录、主窗口未最小化，且微信版本为 4.1.12+")
        ok = False

    try:
        data = http_json("GET", f"{config.gateway.rstrip('/')}/wechat/state", None, config.token, 5)
        print(f"✅ 网关可达：{data.get('account')}")
    except Exception as exc:
        print(f"❌ 网关不可达：{exc}")
        print("   检查 Windows 防火墙、gateway 地址和端口（bridge 在 9002）")
        ok = False

    print("== 自检结束 ==" if ok else "== 自检失败 ==")
    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="wechat-onebot Windows 桥接代理")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--doctor", action="store_true", help="只做环境自检")
    parser.add_argument("--mock", action="store_true", help="不启动微信，用假消息验证桥接")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging(config, args.verbose)

    if args.doctor:
        return doctor(config)

    if args.mock:
        wechat = MockWeChat()
        agent = BridgeAgent(config, wechat)
        agent.start()
        time.sleep(1)
        wechat.emit("123456@chatroom", "mock：@机器人 你好", sender_id="wxid_alice")
        try:
            agent.run()
        except KeyboardInterrupt:
            agent.stop()
        return 0

    from_real = RealWeChat()
    agent = BridgeAgent(config, from_real)
    try:
        agent.start()
        agent.run()
    except KeyboardInterrupt:
        agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
