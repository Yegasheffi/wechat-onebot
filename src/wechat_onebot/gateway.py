"""网关：把驱动、OneBot 协议、网络端点、媒体、WebUI 串起来。"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import mimetypes
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .actions import ActionHandler
from .config import ConfigStore
from .drivers import create_driver
from .events import (
    heartbeat_event,
    lifecycle_event,
    message_event,
    notice_event,
    render_for_format,
)
from .httpkit import HttpError, RawRequest, Response, json_response
from .httpserver import HttpServer
from .logger import get_logger
from .model import ChatMessage, LoginInfo, Notice, Segment, Sender
from .segments import to_plain_text
from .store import MessageStore, StoredMessage
from .wsproto import WebSocket, WebSocketServer, connect as ws_connect

log = get_logger("gateway")


class StartupError(RuntimeError):
    """关键端点启动失败，网关不应带着残废状态继续运行。"""


class MediaStore:
    """内存媒体缓存：驱动把图片/语音字节丢进来，换一个可被 Koishi 拉取的 URL。"""

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self._items: Dict[str, Tuple[bytes, str]] = {}
        self._order: List[str] = []

    def put(self, data: bytes, filename: str = "", content_type: str = "") -> str:
        digest = hashlib.sha1(data).hexdigest()[:16]
        suffix = ""
        if filename and "." in filename:
            suffix = "." + filename.rsplit(".", 1)[-1].lower()[:8]
        else:
            guessed = mimetypes.guess_extension(content_type or "") or ""
            suffix = guessed
        name = f"{digest}{suffix}"
        self._items[name] = (data, content_type or mimetypes.guess_type(name)[0] or "application/octet-stream")
        self._order.append(name)
        while len(self._order) > self.capacity:
            oldest = self._order.pop(0)
            self._items.pop(oldest, None)
        return name

    def get(self, name: str) -> Optional[Tuple[bytes, str]]:
        return self._items.get(name)

    def clear(self) -> None:
        self._items.clear()
        self._order.clear()

    def stats(self) -> dict:
        return {"cached": len(self._items), "capacity": self.capacity}


@dataclass
class ClientInfo:
    key: str
    role: str  # forward-ws / reverse-ws / http
    name: str
    post_format: str = "array"
    report_self_message: bool = False
    debug: bool = False
    heart_interval: int = 30000
    connected_at: int = field(default_factory=lambda: int(time.time()))


class OneBotClient:
    """一个 OneBot 连接（正向 WS 或反向 WS）。"""

    def __init__(self, gateway: "Gateway", ws: WebSocket, cfg: Dict[str, Any], role: str, name: str) -> None:
        self.gateway = gateway
        self.ws = ws
        self.cfg = cfg
        self.info = ClientInfo(
            key=f"{role}:{name}:{id(ws)}",
            role=role,
            name=name,
            post_format=str(cfg.get("messagePostFormat") or "array"),
            report_self_message=bool(cfg.get("reportSelfMessage")),
            debug=bool(cfg.get("debug")),
            heart_interval=int(cfg.get("heartInterval") or 30000),
        )
        self._closed = False

    async def send_payload(self, payload: Dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
            self.gateway.stats["sent"] += 1
        except Exception as exc:
            log.debug("向客户端 %s 发送失败: %s", self.info.name, exc)

    async def send_event(self, event: Dict[str, Any]) -> None:
        if event.get("post_type") == "message_sent" and not self.info.report_self_message:
            return
        await self.send_payload(render_for_format(event, self.info.post_format))

    async def run(self) -> None:
        await self.send_payload(lifecycle_event(self.gateway.self_id, "connect"))
        while not self._closed:
            message = await self.ws.recv()
            if message is None:
                break
            if isinstance(message, bytes):
                continue
            await self.gateway.handle_client_text(self, message)
        self._closed = True
        self.gateway.remove_client(self.info.key)
        log.info("客户端断开：%s", self.info.name)

    async def close(self) -> None:
        self._closed = True
        await self.ws.close(1001, "网关关闭")

    async def handle_action(self, payload: Dict[str, Any]) -> None:
        action = str(payload.get("action") or "")
        params = payload.get("params") or {}
        echo = payload.get("echo")
        self.gateway.stats["received"] += 1
        if self.info.debug:
            log.debug("[%s] <- %s %s", self.info.name, action, params)
        response = await self.gateway.actions.dispatch(action, params, echo)
        await self.send_payload(response)


class ReverseClient:
    """反向 WebSocket：本程序主动连到 Koishi（ws-reverse 模式）。"""

    def __init__(self, gateway: "Gateway", cfg: Dict[str, Any]) -> None:
        self.gateway = gateway
        self.cfg = cfg
        self.name = str(cfg.get("name") or cfg.get("url") or "reverse")
        self.url = str(cfg.get("url") or "")
        self.token = str(cfg.get("token") or "")
        self.reconnect = max(1000, int(cfg.get("reconnectInterval") or 3000)) / 1000
        self.client: Optional[OneBotClient] = None
        self._stopped = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            self._task = None
        if self.client:
            await self.client.close()
            self.client = None

    async def restart(self) -> None:
        await self.stop()
        self._stopped = False
        self.start()

    async def _run(self) -> None:
        while not self._stopped:
            try:
                headers = {
                    "X-Self-ID": str(self.gateway.self_id),
                    "X-Client-Role": "Universal",
                    "User-Agent": f"wechat-onebot/{__version__}",
                }
                if self.token:
                    headers["Authorization"] = f"Token {self.token}"
                connection = await ws_connect(self.url, headers=headers, timeout=10)
                log.info("反向 WS 已连接：%s", self.url)
                client = OneBotClient(self.gateway, connection.ws, self.cfg, "reverse-ws", self.name)
                self.client = client
                self.gateway.add_client(client)
                await client.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("反向 WS 连接失败（%s）：%s", self.url, exc)
            finally:
                if self.client:
                    self.gateway.remove_client(self.client.info.key)
                    self.client = None
            if self._stopped:
                return
            await asyncio.sleep(self.reconnect)


class Gateway:
    def __init__(self, config: ConfigStore) -> None:
        self.config = config
        self.store = MessageStore()
        self.media = MediaStore()
        self.actions = ActionHandler(self)
        self.self_id: str = config.account().get("wxid") or "10000"
        self.nickname: str = config.account().get("nickname") or "微信机器人"
        self.online = False
        self.driver = None  # type: ignore[assignment]
        self.ws_servers: List[WebSocketServer] = []
        self.reverse_clients: List[ReverseClient] = []
        self.http_servers: List[HttpServer] = []
        self.aux_server: Optional[HttpServer] = None
        self.webui = None  # type: ignore[assignment]
        self.clients: Dict[str, OneBotClient] = {}
        self.stats = {"received": 0, "sent": 0, "messages": 0, "sent_messages": 0, "notices": 0}
        self._aux_routes: List[Tuple[str, str, Any]] = []
        self._event_subscribers: List[asyncio.Queue] = []
        self.recent_events = collections.deque(maxlen=120)
        self._tasks: set = set()
        self._startup_errors: List[tuple] = []
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.media_server: Optional[HttpServer] = None
        self.media_base = ""
        self.started_at = int(time.time())

    # -- 供驱动使用的上下文 ------------------------------------------------
    @property
    def log(self):
        return log

    def register_route(self, method: str, pattern: str, handler) -> None:
        """驱动注册回调路由（iPad 回调 / Webhook 桥）。"""
        self._aux_routes.append((method.upper(), pattern, handler))

    def media_url(self, data: bytes, filename: str = "", content_type: str = "") -> str:
        if not self.media_base:
            return ""
        name = self.media.put(data, filename, content_type)
        return f"{self.media_base.rstrip('/')}/media/{name}"

    @property
    def logged_in(self) -> bool:
        if self.driver is None:
            return False
        try:
            return bool(self.driver.login_state().get("logged_in", True))
        except Exception:
            return True

    def login_state(self) -> Dict[str, Any]:
        if self.driver is None:
            return {"supported": False, "logged_in": False, "account": {}, "qr": None}
        try:
            return self.driver.login_state()
        except Exception as exc:
            return {"supported": False, "logged_in": False, "account": {}, "qr": None, "error": str(exc)}

    async def set_self_id(self, wxid: str, nickname: str = "") -> None:
        changed = bool(wxid) and wxid != self.self_id
        if changed:
            log.info("账号信息更新：%s -> %s", self.self_id, wxid)
            self.self_id = wxid
        if nickname:
            self.nickname = nickname
        if changed:
            # 反向 WS 的 X-Self-ID 在握手时固定，账号变化后需要重连一次
            for reverse in self.reverse_clients:
                try:
                    await reverse.restart()
                except Exception as exc:
                    log.debug("反向 WS 重连失败：%s", exc)

    # -- 生命周期 ----------------------------------------------------------
    async def start(self) -> None:
        driver_name = self.config.driver_name
        log.info("启动微信驱动：%s", driver_name)
        self.driver = create_driver(driver_name, self.config.wechat, self)

        # 1) 回调 HTTP 服务（驱动注册路由）
        callback_cfg = self.config.wechat.get("callback") or {}
        if callback_cfg.get("enable", True):
            self.aux_server = HttpServer(
                str(callback_cfg.get("host") or "0.0.0.0"),
                int(callback_cfg.get("port") or 9002),
                name="callback",
            )
            self.aux_server.add_route("GET", "/health", self._health_handler)
            for method, pattern, handler in self._aux_routes:
                self.aux_server.add_route(method, pattern, handler)

        # 2) 驱动上线
        await self.driver.start()
        try:
            info: LoginInfo = await self.driver.login()
            if info.user_id:
                await self.set_self_id(info.user_id, info.nickname)
        except Exception as exc:
            log.warning("驱动登录失败（网关继续启动）：%s", exc)
        self.online = True

        # 3) 回调服务启动
        if self.aux_server is not None:
            for method, pattern, handler in self._aux_routes:
                if not any(r.pattern == pattern and r.method == method for r in self.aux_server.routes):
                    self.aux_server.add_route(method, pattern, handler)
            await self._guarded_start(self.aux_server, "回调服务")

        # 4) 媒体服务
        await self._start_media()

        # 5) OneBot 网络端点
        await self._start_network()

        # 6) WebUI
        webui_cfg = self.config.webui_config()
        if webui_cfg.get("enable", True):
            from .webui import WebUI

            self.webui = WebUI(self, webui_cfg)
            try:
                await self.webui.start()
            except OSError as exc:
                log.error("WebUI 启动失败：%s（端口可能被占用，改 webui.json 的 port 后重启）", exc)
                self._startup_errors.append(("WebUI", str(exc)))

        # 7) 启动完整性检查：关键端点缺失时直接失败，避免"看似启动、实际不可用"
        critical: List[str] = []
        webui_cfg = self.config.webui_config()
        if webui_cfg.get("enable", True) and any(label == "WebUI" for label, _ in self._startup_errors):
            critical.append("WebUI")
        callback_cfg = self.config.wechat.get("callback") or {}
        if (
            callback_cfg.get("enable", True)
            and self.driver.name in ("ipad", "bridge")
            and any(label == "回调服务" for label, _ in self._startup_errors)
        ):
            critical.append("驱动回调服务")
        if not self.ws_servers and not self.reverse_clients and not self.http_servers:
            critical.append("OneBot 连接端点")
        if critical:
            for label, detail in self._startup_errors:
                log.error("· %s：%s", label, detail)
            log.error("启动不完整，缺少：%s", "、".join(critical))
            log.error("请先执行 ./start.sh --stop 停止旧实例，或修改对应端口后重试")
            raise StartupError("关键端点启动失败：" + "、".join(critical))

        # 8) 心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        log.info("网关已就绪：账号 %s(%s)，驱动 %s", self.nickname, self.self_id, self.driver.name)

    async def stop(self) -> None:
        log.info("正在关闭网关…")
        self._stop_event.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        for client in list(self.clients.values()):
            await client.close()
        self.clients.clear()
        for reverse in self.reverse_clients:
            await reverse.stop()
        for server in self.ws_servers:
            await server.stop()
        for server in self.http_servers:
            await server.stop()
        if self.media_server:
            await self.media_server.stop()
        if self.aux_server:
            await self.aux_server.stop()
        if self.webui:
            await self.webui.stop()
        if self.driver:
            try:
                await self.driver.stop()
            except Exception as exc:
                log.debug("驱动停止异常: %s", exc)
        self.online = False
        log.info("已退出")

    async def run_forever(self) -> None:
        try:
            await self.start()
            await self._stop_event.wait()
        finally:
            await self.stop()

    def request_stop(self) -> None:
        self._stop_event.set()

    # -- 网络端点 ----------------------------------------------------------
    async def _start_network(self) -> None:
        network = (self.config.onebot11.get("network") or {})
        for cfg in network.get("websocketServers") or []:
            if not cfg.get("enable", False):
                continue
            host = str(cfg.get("host") or "0.0.0.0")
            port = int(cfg.get("port") or 3001)
            path = str(cfg.get("path") or "/onebot")
            name = str(cfg.get("name") or f"ws-{port}")
            token = str(cfg.get("token") or "")

            def verify(request: RawRequest, token: str = token) -> Optional[str]:
                return self._check_ws_token(request, token)

            async def on_connect(ws: WebSocket, request: RawRequest, cfg: Dict[str, Any] = cfg, name: str = name) -> None:
                await self._serve_ws(ws, cfg, "forward-ws", name)

            server = WebSocketServer(host, port, path, on_connect, verify=verify, name=name)
            if await self._guarded_start(server, f"正向 WS [{name}]"):
                self.ws_servers.append(server)

        for cfg in network.get("websocketClients") or []:
            if not cfg.get("enable", False) or not cfg.get("url"):
                continue
            reverse = ReverseClient(self, cfg)
            self.reverse_clients.append(reverse)
            reverse.start()

        for cfg in network.get("httpServers") or []:
            if not cfg.get("enable", False):
                continue
            server = HttpServer(str(cfg.get("host") or "0.0.0.0"), int(cfg.get("port") or 5700), name=str(cfg.get("name") or "onebot-http"))
            token = str(cfg.get("token") or "")

            async def action_handler(request: RawRequest, token: str = token) -> Response:
                self._check_http_token(request, token)
                action = str(request.params.get("action") or "")
                body = request.json()
                params = body.get("params") if isinstance(body, dict) and "params" in body else body
                result = await self.actions.dispatch(action, params if isinstance(params, dict) else {})
                return json_response(result)

            server.add_route("POST", "/{action}", action_handler)
            server.add_route("GET", "/media/{name}", self._media_handler)
            server.add_route("GET", "/", self._root_handler)
            if await self._guarded_start(server, f"HTTP API [{server.name}]"):
                self.http_servers.append(server)

        if self.media_server is None:
            log.warning("未启动媒体服务，图片/语音将以 base64 或原始路径下发")

    async def _guarded_start(self, server, label: str) -> bool:
        try:
            await server.start()
            return True
        except OSError as exc:
            log.error(
                "%s 启动失败：%s（端口可能被占用，请改端口后重启）",
                label,
                exc,
            )
            self._startup_errors.append((label, str(exc)))
            return False

    async def _start_media(self) -> None:
        if not self.config.onebot11.get("enableLocalFile2Url", True):
            log.info("enableLocalFile2Url=false，媒体服务不启动")
            return
        media_cfg = self.config.wechat.get("media") or {}
        if not media_cfg.get("enable", True):
            return
        if self.media_server is not None:
            return
        port = int(media_cfg.get("port") or 3002)
        host = str(media_cfg.get("host") or "0.0.0.0")
        server = HttpServer(host, port, name="media")
        server.add_route("GET", "/media/{name}", self._media_handler)
        server.add_route("GET", "/", self._root_handler)
        try:
            await server.start()
            self.media_server = server
        except OSError as exc:
            log.warning("媒体服务端口 %s 启动失败：%s", port, exc)
            return
        public_base = str(media_cfg.get("public_base") or "").strip()
        if not public_base:
            public_base = f"http://127.0.0.1:{port}"
        self.media_base = public_base
        log.info("媒体服务：%s/media/", public_base)

    def _check_ws_token(self, request: RawRequest, token: str) -> Optional[str]:
        if not token:
            return None
        auth = request.header("authorization")
        candidates = []
        if auth.lower().startswith("bearer "):
            candidates.append(auth[7:].strip())
        elif auth.lower().startswith("token "):
            candidates.append(auth[6:].strip())
        candidates.append(request.header("x-onebot-token"))
        candidates.append(request.query_one("access_token"))
        if any(candidate == token for candidate in candidates if candidate):
            return None
        return "token 校验失败"

    def _check_http_token(self, request: RawRequest, token: str) -> None:
        if not token:
            return
        auth = request.header("authorization")
        candidates = []
        if auth.lower().startswith("bearer "):
            candidates.append(auth[7:].strip())
        elif auth.lower().startswith("token "):
            candidates.append(auth[6:].strip())
        candidates.append(request.header("x-onebot-token"))
        candidates.append(request.query_one("access_token"))
        if not any(candidate == token for candidate in candidates if candidate):
            raise HttpError(401, "token 校验失败")

    async def _serve_ws(self, ws: WebSocket, cfg: Dict[str, Any], role: str, name: str) -> None:
        client = OneBotClient(self, ws, cfg, role, name)
        self.add_client(client)
        try:
            await client.run()
        finally:
            self.remove_client(client.info.key)

    # -- 客户端管理 --------------------------------------------------------
    def add_client(self, client: OneBotClient) -> None:
        self.clients[client.info.key] = client

    def remove_client(self, key: str) -> None:
        self.clients.pop(key, None)

    def client_summary(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": info.name,
                "role": info.role,
                "post_format": info.post_format,
                "report_self_message": info.report_self_message,
                "connected_at": info.connected_at,
            }
            for info in (client.info for client in self.clients.values())
        ]

    # -- 事件 --------------------------------------------------------------
    async def handle_client_text(self, client: OneBotClient, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log.warning("收到无法解析的 JSON：%s", text[:200])
            return
        if not isinstance(payload, dict):
            return
        if "action" in payload:
            await client.handle_action(payload)
            return
        if "post_type" in payload:
            # 客户端主动上报的事件，忽略
            return

    async def broadcast(self, event: Dict[str, Any]) -> None:
        for client in list(self.clients.values()):
            try:
                await client.send_event(event)
            except Exception as exc:
                log.debug("广播到 %s 失败: %s", client.info.name, exc)
        self._push_webui_event(event)
        for cfg in (self.config.onebot11.get("network") or {}).get("httpClients") or []:
            if not cfg.get("enable", False) or not cfg.get("url"):
                continue
            if event.get("post_type") == "message_sent" and not cfg.get("reportSelfMessage"):
                continue
            fmt = str(cfg.get("messagePostFormat") or "array")
            body = render_for_format(event, fmt)
            task = asyncio.create_task(self._post_http_client(cfg, body))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _post_http_client(self, cfg: Dict[str, Any], body: Dict[str, Any]) -> None:
        url = str(cfg.get("url"))
        secret = str(cfg.get("secret") or "")
        timeout = max(1.0, float(cfg.get("timeout") or 5000) / 1000)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Self-ID": str(self.self_id)}
        if secret:
            headers["X-Signature"] = "sha1=" + hmac.new(secret.encode(), data, hashlib.sha1).hexdigest()

        def _send() -> None:
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()

        try:
            await asyncio.to_thread(_send)
        except Exception as exc:
            log.debug("HTTP 事件上报 %s 失败：%s", url, exc)

    def event_for_message(self, stored: Optional[StoredMessage], post_format: str = "array") -> Dict[str, Any]:
        if stored is None:
            return {}
        message = stored.message
        quote_id = None
        if message.quote_platform_id:
            quoted = self.store.by_platform(str(message.quote_platform_id))
            if quoted:
                quote_id = quoted.onebot_id
        return message_event(
            message,
            stored.onebot_id,
            self.self_id,
            post_format=post_format,
            is_self_sent=message.is_self,
            target_id=message.chat_id,
            quote_id=quote_id,
        )

    async def emit_message(self, message: ChatMessage) -> None:
        if message.platform_id and self.store.by_platform(str(message.platform_id)):
            log.debug("重复消息已忽略：%s", message.platform_id)
            return
        if message.is_self:
            await self.emit_self_message(message)
            return
        onebot_id = self.store.remember(message)
        self.stats["messages"] += 1
        stored = self.store.get(onebot_id)
        event = self.event_for_message(stored)
        log.info(
            "收到消息 %s(%s) <- %s: %s",
            message.chat_type,
            message.chat_id,
            message.sender.nickname or message.sender.user_id,
            to_plain_text(message.segments) or "[富媒体]",
        )
        await self.broadcast(event)

    async def emit_self_message(self, message: ChatMessage) -> None:
        if message.platform_id and self.store.by_platform(str(message.platform_id)):
            return
        message.is_self = True
        onebot_id = self.store.remember(message)
        self.stats["sent_messages"] += 1
        stored = self.store.get(onebot_id)
        event = self.event_for_message(stored)
        await self.broadcast(event)

    async def record_self_message(
        self, chat_type: str, chat_id: str, segments: List[Segment], platform_id: Optional[str]
    ) -> int:
        message = ChatMessage(
            platform_id=str(platform_id or f"self-{int(time.time() * 1000)}"),
            chat_type=chat_type,
            chat_id=str(chat_id),
            sender=Sender(self.self_id, self.nickname, "", role="member"),
            segments=segments,
            time=int(time.time()),
            is_self=True,
        )
        await self.emit_self_message(message)
        stored = self.store.by_platform(message.platform_id)
        return stored.onebot_id if stored else 0

    async def emit_notice(self, notice: Notice) -> None:
        # 微信侧的撤回通知带的是平台消息 ID，转成 OneBot message_id 再下发
        if notice.message_id:
            stored = self.store.by_platform(str(notice.message_id))
            if stored:
                notice.message_id = str(stored.onebot_id)
        event = notice_event(notice, self.self_id)
        self.stats["notices"] += 1
        await self.broadcast(event)

    async def emit_request(self, request: Dict[str, Any]) -> None:
        from .events import friend_request_event, group_request_event

        kind = request.get("type")
        if kind == "friend":
            event = friend_request_event(
                str(request.get("user_id", "")), str(request.get("comment", "")), str(request.get("flag", "")), self.self_id
            )
        else:
            event = group_request_event(
                str(request.get("group_id", "")),
                str(request.get("user_id", "")),
                str(request.get("comment", "")),
                str(request.get("flag", "")),
                str(request.get("sub_type") or "add"),
                self.self_id,
            )
        await self.broadcast(event)

    async def emit_login(self, info: LoginInfo) -> None:
        await self.set_self_id(info.user_id, info.nickname)

    # -- 心跳 --------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        intervals = [client.info.heart_interval for client in self.clients.values() if client.info.heart_interval > 0]
        for cfg in (self.config.onebot11.get("network") or {}).get("websocketServers") or []:
            if cfg.get("enable", False):
                intervals.append(int(cfg.get("heartInterval") or 30000))
        for cfg in (self.config.onebot11.get("network") or {}).get("websocketClients") or []:
            if cfg.get("enable", False):
                intervals.append(int(cfg.get("heartInterval") or 30000))
        interval = max(1000, min(intervals or [30000])) / 1000
        log.debug("心跳间隔 %.1fs", interval)
        while True:
            await asyncio.sleep(interval)
            event = heartbeat_event(self.self_id, int(interval * 1000))
            for client in list(self.clients.values()):
                await client.send_event(event)

    # -- WebUI 事件总线 ----------------------------------------------------
    def subscribe_events(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=300)
        self._event_subscribers.append(queue)
        return queue

    def unsubscribe_events(self, queue: asyncio.Queue) -> None:
        if queue in self._event_subscribers:
            self._event_subscribers.remove(queue)

    def _push_webui_event(self, event: Dict[str, Any]) -> None:
        self.recent_events.append(event)
        for queue in list(self._event_subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # -- HTTP 处理 ---------------------------------------------------------
    async def _health_handler(self, request: RawRequest) -> Response:
        return json_response(
            {
                "ok": True,
                "app": "wechat-onebot",
                "version": __version__,
                "driver": self.driver.name if self.driver else None,
                "online": self.online and self.logged_in,
                "logged_in": self.logged_in,
                "account": {"user_id": self.self_id, "nickname": self.nickname},
            }
        )

    async def _root_handler(self, request: RawRequest) -> Response:
        return json_response(
            {
                "app": "wechat-onebot",
                "version": __version__,
                "account": {"user_id": self.self_id, "nickname": self.nickname},
                "driver": self.driver.name if self.driver else None,
                "clients": len(self.clients),
            }
        )

    async def _media_handler(self, request: RawRequest) -> Response:
        name = str(request.params.get("name") or "")
        item = self.media.get(name)
        if item is None:
            raise HttpError(404, "媒体不存在")
        data, content_type = item
        return Response(status=200, body=data, content_type=content_type, headers={"Cache-Control": "max-age=60"})

    # -- 状态（WebUI 用） --------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "online": self.online and self.logged_in,
            "account": {"user_id": self.self_id, "nickname": self.nickname},
            "driver": self.driver.name if self.driver else None,
            "config_dir": str(self.config.dir),
            "uptime": int(time.time()) - self.started_at,
            "clients": self.client_summary(),
            "stats": dict(self.stats),
            "store": self.store.stats(),
            "media": self.media.stats(),
            "endpoints": self._endpoint_summary(),
            "version": __version__,
            "logged_in": self.logged_in,
            "wechat_login": self.login_state(),
        }

    def _endpoint_summary(self) -> Dict[str, List[str]]:
        ws_urls = [f"ws://{server.host}:{server.port}{server.path}" for server in self.ws_servers]
        http_urls = [f"http://{server.host}:{server.port}" for server in self.http_servers]
        reverse_urls = [client.url for client in self.reverse_clients]
        return {"websocket": ws_urls, "http": http_urls, "reverse": reverse_urls, "media": [self.media_base] if self.media_base else []}
