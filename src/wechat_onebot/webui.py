"""WebUI：苹果（macOS）风格的控制台。状态 / 模拟消息 / 网络配置 / 实时日志。"""

from __future__ import annotations

import asyncio
import hmac
import time
from pathlib import Path
from typing import Any, Dict, List

from . import __version__
from .httpkit import HttpError, RawRequest, Response, SSEStream, json_response
from .httpserver import HttpServer
from .logger import get_logger, recent_logs, subscribe_logs
from .model import DriverError, UnsupportedAction
from .segments import normalize_message, to_plain_text

log = get_logger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "wo_token"


class WebUI:
    def __init__(self, gateway, config: Dict[str, Any]) -> None:
        self.gateway = gateway
        self.config = config
        self.host = str(config.get("host") or "0.0.0.0")
        self.port = int(config.get("port") or 6099)
        self.token = str(config.get("token") or "")
        self.server = HttpServer(self.host, self.port, name="webui")
        self._sse_tasks: set = set()
        self._build_routes()

    # -- 路由 --------------------------------------------------------------
    def _build_routes(self) -> None:
        server = self.server
        server.add_route("POST", "/api/login", self._login)
        server.add_route("POST", "/api/logout", self._logout)
        server.add_route("GET", "/api/status", self._status)
        server.add_route("GET", "/api/config", self._get_config)
        server.add_route("POST", "/api/config", self._save_config)
        server.add_route("GET", "/api/logs", self._logs)
        server.add_sse("/api/logs/stream", self._log_stream)
        server.add_sse("/api/events/stream", self._event_stream)
        server.add_route("GET", "/api/events/recent", self._recent_events)
        server.add_route("GET", "/api/mock/state", self._mock_state)
        server.add_route("POST", "/api/mock/message", self._mock_message)
        server.add_route("GET", "/api/drivers", self._drivers)
        server.add_route("GET", "/api/login/state", self._login_state)
        server.add_route("POST", "/api/login/qr", self._login_qr)
        server.add_route("GET", "/api/login/check", self._login_check)
        server.add_route("POST", "/api/login/logout", self._login_logout)
        server.add_static("/", STATIC_DIR)

    # -- 生命周期 ----------------------------------------------------------
    async def start(self) -> None:
        await self.server.start()
        if self.token:
            log.info("WebUI: http://127.0.0.1:%s  （登录令牌：%s）", self.port, self.token)
        else:
            log.info("WebUI: http://127.0.0.1:%s  （未设置令牌，任何本机用户可访问）", self.port)

    async def stop(self) -> None:
        for task in list(self._sse_tasks):
            task.cancel()
        await self.server.stop()

    # -- 鉴权 --------------------------------------------------------------
    def _authenticated(self, request: RawRequest) -> bool:
        if not self.token:
            return True
        supplied = request.header("x-token") or request.query_one("token")
        if not supplied:
            cookie = request.header("cookie")
            for part in cookie.split(";"):
                key, _, value = part.strip().partition("=")
                if key == COOKIE_NAME:
                    supplied = value
                    break
        return bool(supplied) and hmac.compare_digest(str(supplied), self.token)

    def _require(self, request: RawRequest) -> None:
        if not self._authenticated(request):
            raise HttpError(401, "未登录或令牌错误")

    async def _login(self, request: RawRequest) -> Response:
        body = request.json()
        supplied = str((body or {}).get("token") or "")
        if self.token and not hmac.compare_digest(supplied, self.token):
            raise HttpError(403, "令牌错误")
        response = json_response({"ok": True})
        if self.token:
            response.headers["Set-Cookie"] = f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"
        return response

    async def _logout(self, request: RawRequest) -> Response:
        response = json_response({"ok": True})
        response.headers["Set-Cookie"] = f"{COOKIE_NAME}=; Path=/; Max-Age=0"
        return response

    # -- 状态 --------------------------------------------------------------
    async def _status(self, request: RawRequest) -> Response:
        self._require(request)
        status = self.gateway.status()
        status["driver_status"] = await self._driver_status()
        status["webui"] = {"port": self.port, "config_dir": str(self.gateway.config.dir)}
        return json_response(status)

    async def _driver_status(self) -> Dict[str, Any]:
        try:
            return await self.gateway.driver.status() or {}
        except Exception as exc:
            return {"error": str(exc)}

    async def _drivers(self, request: RawRequest) -> Response:
        self._require(request)
        from .drivers import DRIVER_NAMES

        return json_response({"current": self.gateway.config.driver_name, "available": DRIVER_NAMES})

    # -- 配置 --------------------------------------------------------------
    async def _get_config(self, request: RawRequest) -> Response:
        self._require(request)
        wechat = self.gateway.config.raw("wechat")
        webui = self.gateway.config.raw("webui")
        webui = {k: v for k, v in webui.items() if k != "token"}
        return json_response(
            {
                "onebot11": self.gateway.config.raw("onebot11"),
                "wechat": wechat,
                "webui": webui,
                "paths": {name: str(path) for name, path in self.gateway.config.paths.items()},
            }
        )

    async def _save_config(self, request: RawRequest) -> Response:
        self._require(request)
        body = request.json()
        name = str((body or {}).get("name") or "")
        data = (body or {}).get("data")
        if name not in ("onebot11", "wechat", "webui"):
            raise HttpError(400, f"未知配置: {name}")
        if not isinstance(data, dict):
            raise HttpError(400, "配置必须是 JSON 对象")
        if name == "webui" and not data.get("token"):
            data["token"] = self.token
        try:
            self.gateway.config.update_raw(name, data)
        except (OSError, ValueError) as exc:
            raise HttpError(400, f"保存失败: {exc}") from exc
        self.gateway.config.load()
        log.info("配置 %s 已保存（部分改动需要重启进程生效）", name)
        return json_response({"ok": True, "restart_required": name in ("onebot11", "wechat")})

    # -- 日志 --------------------------------------------------------------
    async def _logs(self, request: RawRequest) -> Response:
        self._require(request)
        limit = min(500, max(1, int(request.query_one("limit") or 200)))
        return json_response({"logs": recent_logs(limit)})

    async def _log_stream(self, request: RawRequest, stream: SSEStream) -> None:
        if not self._authenticated(request):
            await stream.send("error", {"message": "未登录"})
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        loop = asyncio.get_running_loop()

        def on_log(item: Dict[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except Exception:
                pass

        unsubscribe = subscribe_logs(on_log)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    if not await stream.send("log", item):
                        return
                except asyncio.TimeoutError:
                    if not await stream.comment("ping"):
                        return
        finally:
            unsubscribe()

    # -- 事件流 ------------------------------------------------------------
    async def _event_stream(self, request: RawRequest, stream: SSEStream) -> None:
        if not self._authenticated(request):
            await stream.send("error", {"message": "未登录"})
            return
        queue = self.gateway.subscribe_events()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    if not await stream.send("event", self._event_view(event)):
                        return
                except asyncio.TimeoutError:
                    if not await stream.comment("ping"):
                        return
        finally:
            self.gateway.unsubscribe_events(queue)

    async def _recent_events(self, request: RawRequest) -> Response:
        self._require(request)
        events = [self._event_view(event) for event in list(self.gateway.recent_events)[-80:]]
        return json_response({"events": events})

    def _event_view(self, event: Dict[str, Any]) -> Dict[str, Any]:
        view = {
            "post_type": event.get("post_type"),
            "message_type": event.get("message_type"),
            "sub_type": event.get("sub_type") or event.get("notice_type"),
            "user_id": event.get("user_id"),
            "group_id": event.get("group_id"),
            "target_id": event.get("target_id"),
            "time": event.get("time"),
            "message_id": event.get("message_id"),
        }
        segments = event.get("message")
        if isinstance(segments, list):
            view["segments"] = segments
            view["text"] = to_plain_text(normalize_message(segments))
        elif isinstance(segments, str):
            view["text"] = to_plain_text(normalize_message(segments))
        sender = event.get("sender") or {}
        view["sender_name"] = sender.get("card") or sender.get("nickname") or view.get("user_id")
        return view

    # -- 微信登录 ----------------------------------------------------------
    async def _login_state(self, request: RawRequest) -> Response:
        self._require(request)
        return json_response(
            {
                "state": self.gateway.login_state(),
                "driver": self.gateway.driver.name if self.gateway.driver else None,
                "logged_in": self.gateway.logged_in,
            }
        )

    async def _login_qr(self, request: RawRequest) -> Response:
        self._require(request)
        driver = self.gateway.driver
        if driver is None or not getattr(driver, "supports_login", False):
            raise HttpError(400, f"当前驱动（{getattr(driver, 'name', '?')}）不需要扫码登录")
        try:
            qr = await driver.create_login_qr()
        except UnsupportedAction as exc:
            raise HttpError(400, str(exc)) from exc
        except DriverError as exc:
            raise HttpError(502, str(exc)) from exc
        return json_response({"ok": True, "qr": qr, "state": self.gateway.login_state()})

    async def _login_check(self, request: RawRequest) -> Response:
        self._require(request)
        driver = self.gateway.driver
        if driver is None or not getattr(driver, "supports_login", False):
            raise HttpError(400, "当前驱动不需要扫码登录")
        qr_uuid = request.query_one("uuid")
        try:
            result = await driver.check_login_qr(qr_uuid)
        except UnsupportedAction as exc:
            raise HttpError(400, str(exc)) from exc
        except DriverError as exc:
            raise HttpError(502, str(exc)) from exc
        return json_response({"ok": True, "result": result, "state": self.gateway.login_state()})

    async def _login_logout(self, request: RawRequest) -> Response:
        self._require(request)
        driver = self.gateway.driver
        if driver is None or not getattr(driver, "supports_login", False):
            raise HttpError(400, "当前驱动不需要退出登录")
        try:
            state = await driver.logout()
        except UnsupportedAction as exc:
            raise HttpError(400, str(exc)) from exc
        except DriverError as exc:
            raise HttpError(502, str(exc)) from exc
        return json_response({"ok": True, "state": state})

    # -- 模拟器 ------------------------------------------------------------
    async def _mock_state(self, request: RawRequest) -> Response:
        self._require(request)
        driver = self.gateway.driver
        if driver is None or driver.name != "mock":
            return json_response({"available": False, "driver": getattr(driver, "name", None)})
        friends = [{"user_id": f.user_id, "name": f.display_name()} for f in await driver.get_friend_list()]
        groups = []
        for group in await driver.get_group_list():
            members = [{"user_id": m.user_id, "name": m.display_name()} for m in await driver.get_group_members(group.group_id)]
            groups.append({"group_id": group.group_id, "name": group.name, "members": members})
        return json_response(
            {
                "available": True,
                "account": {"user_id": self.gateway.self_id, "nickname": self.gateway.nickname},
                "friends": friends,
                "groups": groups,
            }
        )

    async def _mock_message(self, request: RawRequest) -> Response:
        self._require(request)
        driver = self.gateway.driver
        if driver is None or driver.name != "mock":
            raise HttpError(400, "当前驱动不是 mock，无法模拟消息")
        body = request.json() or {}
        chat_type = str(body.get("chat_type") or "group")
        chat_id = str(body.get("chat_id") or "")
        text = str(body.get("text") or "").strip()
        sender_id = str(body.get("sender_id") or "")
        if not chat_id:
            raise HttpError(400, "缺少 chat_id")
        if not text:
            raise HttpError(400, "消息内容为空")
        await driver.inject_message(chat_type, chat_id, text=text, sender_id=sender_id or None)
        return json_response({"ok": True})
