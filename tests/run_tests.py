"""自测：不需要真实 Koishi，也不需要真实微信。

    python3 tests/run_tests.py

覆盖：
  * CQ / 消息段编解码
  * 零依赖 WebSocket 帧收发
  * 网关 + 模拟 Koishi（正向 WS）：登录、收发、历史、撤回、群查询
  * OneBot HTTP API 与 HTTP 事件上报
  * 反向 WS（本程序连 Koishi）
  * WebUI 登录 / 状态 / 模拟消息 / 事件
  * iPad 驱动（本地桩服务）：回调解析、发文本、图片媒体
  * Webhook 桥驱动：事件入站 + 队列出站
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_onebot import wsproto  # noqa: E402
from wechat_onebot.config import ConfigStore  # noqa: E402
from wechat_onebot.gateway import Gateway, StartupError  # noqa: E402
from wechat_onebot.preflight import check as preflight_check  # noqa: E402
from wechat_onebot.httpkit import RawRequest, json_response, read_request  # noqa: E402
from wechat_onebot.httpserver import HttpServer  # noqa: E402
from wechat_onebot.logger import set_level  # noqa: E402
from wechat_onebot.model import Segment  # noqa: E402
from wechat_onebot.segments import from_cq, normalize_message, split_at, to_cq, to_plain_text  # noqa: E402

set_level("WARNING")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_until(predicate, timeout: float = 4.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def http_json(
    method: str,
    url: str,
    body: Any = None,
    headers: Optional[Dict[str, str]] = None,
    cookie: Optional[str] = None,
) -> Tuple[int, Any, Dict[str, str]]:
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    if cookie:
        request_headers["Cookie"] = cookie
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, payload, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", "replace")
        return exc.code, payload, dict(exc.headers)


async def http_json_async(*args, **kwargs) -> Tuple[int, Any, Dict[str, str]]:
    return await asyncio.to_thread(http_json, *args, **kwargs)


class Koishi:
    """模拟 koishi-plugin-adapter-napcat 的 WS 客户端。"""

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token
        self.ws = None
        self.events: asyncio.Queue = asyncio.Queue()
        self._responses: Dict[str, asyncio.Future] = {}
        self._counter = 0
        self._task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        headers = {"User-Agent": "mock-koishi/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        connection = await wsproto.connect(self.url, headers=headers, timeout=5)
        self.ws = connection.ws
        self._task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        while True:
            message = await self.ws.recv()
            if message is None:
                return
            payload = json.loads(message)
            if "post_type" in payload:
                await self.events.put(payload)
            elif payload.get("echo") in self._responses:
                future = self._responses.pop(payload["echo"])
                if not future.done():
                    future.set_result(payload)

    async def call(self, action: str, params: Optional[Dict[str, Any]] = None, timeout: float = 6.0) -> Dict[str, Any]:
        self._counter += 1
        echo = f"t-{self._counter}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._responses[echo] = future
        await self.ws.send_text(json.dumps({"action": action, "params": params or {}, "echo": echo}))
        return await asyncio.wait_for(future, timeout)

    async def event(self, timeout: float = 5.0, of_type: Optional[str] = None) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError("等待事件超时")
            payload = await asyncio.wait_for(self.events.get(), remaining)
            if of_type is None or payload.get("post_type") == of_type:
                return payload

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.ws:
            await self.ws.close()


def build_store(
    directory: Path,
    *,
    driver: str = "mock",
    ws_port: Optional[int] = None,
    ws_token: str = "ws-token",
    report_self: bool = True,
    http_port: Optional[int] = None,
    http_token: str = "http-token",
    push_url: Optional[str] = None,
    reverse_url: Optional[str] = None,
    reverse_token: str = "rev-token",
    callback_port: Optional[int] = None,
    media_port: Optional[int] = None,
    webui_port: Optional[int] = None,
    webui_token: str = "web-token",
    ipad: Optional[Dict[str, Any]] = None,
    bridge: Optional[Dict[str, Any]] = None,
) -> ConfigStore:
    store = ConfigStore(directory / "config")
    onebot = store.raw("onebot11")
    onebot["network"] = {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [],
        "websocketClients": [],
    }
    if ws_port:
        onebot["network"]["websocketServers"].append(
            {
                "enable": True,
                "name": "koishi",
                "host": "127.0.0.1",
                "port": ws_port,
                "path": "/onebot",
                "token": ws_token,
                "messagePostFormat": "array",
                "reportSelfMessage": report_self,
                "heartInterval": 30000,
            }
        )
    if http_port:
        onebot["network"]["httpServers"].append(
            {"enable": True, "name": "api", "host": "127.0.0.1", "port": http_port, "token": http_token}
        )
    if push_url:
        onebot["network"]["httpClients"].append(
            {
                "enable": True,
                "name": "push",
                "url": push_url,
                "secret": "push-secret",
                "messagePostFormat": "array",
                "reportSelfMessage": True,
                "timeout": 3000,
            }
        )
    if reverse_url:
        onebot["network"]["websocketClients"].append(
            {
                "enable": True,
                "name": "koishi-reverse",
                "url": reverse_url,
                "token": reverse_token,
                "messagePostFormat": "array",
                "reportSelfMessage": True,
                "heartInterval": 30000,
                "reconnectInterval": 1000,
            }
        )
    onebot["enableLocalFile2Url"] = True
    store.update_raw("onebot11", onebot)

    wechat = store.raw("wechat")
    wechat["driver"] = driver
    wechat["account"] = {"wxid": "", "nickname": ""}
    wechat["callback"] = {
        "enable": callback_port is not None,
        "host": "127.0.0.1",
        "port": callback_port or 0,
    }
    wechat["mock"]["interactive"] = False
    wechat["media"] = {"enable": media_port is not None, "host": "127.0.0.1", "port": media_port or 0}
    if ipad:
        wechat["ipad"].update(ipad)
    if bridge:
        wechat["bridge"].update(bridge)
    store.update_raw("wechat", wechat)

    webui = store.raw("webui")
    webui["enable"] = webui_port is not None
    webui["host"] = "127.0.0.1"
    webui["port"] = webui_port or 0
    webui["token"] = webui_token
    store.update_raw("webui", webui)
    store.load()
    return store


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.gateways: List[Gateway] = []
        self.clients: List[Koishi] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            try:
                await client.close()
            except Exception:
                pass
        for gateway in self.gateways:
            try:
                await gateway.stop()
            except Exception:
                pass
        self.tmp.cleanup()

    async def start(self, store: ConfigStore) -> Gateway:
        gateway = Gateway(store)
        await gateway.start()
        self.gateways.append(gateway)
        return gateway

    async def koishi(self, port: int, token: str = "ws-token") -> Koishi:
        client = Koishi(f"ws://127.0.0.1:{port}/onebot", token)
        await client.connect()
        self.clients.append(client)
        return client


class TestSegments(unittest.TestCase):
    def test_cq_roundtrip(self):
        segments = normalize_message("你好[CQ:at,qq=wxid_a,name=小爱] [CQ:image,file=http://x/y.png]")
        self.assertEqual(segments[0].type, "text")
        self.assertEqual(segments[1].type, "at")
        self.assertEqual(segments[1].data["qq"], "wxid_a")
        text = to_cq(segments)
        self.assertIn("[CQ:at,qq=wxid_a,name=小爱]", text)
        self.assertEqual(to_plain_text(segments), "你好@小爱 [图片]")

    def test_array_and_escape(self):
        segments = normalize_message([{"type": "text", "data": {"text": "a]b"}}, {"type": "img", "data": {"file": "x"}}])
        self.assertEqual(segments[1].type, "image")
        self.assertEqual(to_cq(segments), "a]b[CQ:image,file=x]")
        escaped = to_cq(segments, auto_escape=True)
        self.assertIn("&#93;", escaped)
        self.assertEqual(to_plain_text(from_cq(escaped)), "a]b[图片]")

    def test_split_at(self):
        ats, rest = split_at(
            [
                Segment("at", {"qq": "wxid_a", "name": "小爱"}),
                Segment("text", {"text": " 在吗"}),
            ]
        )
        self.assertEqual(ats, ["wxid_a"])
        self.assertEqual(to_plain_text(rest), "在吗")


class TestWebSocket(unittest.IsolatedAsyncioTestCase):
    async def test_echo_and_close(self):
        port = free_port()
        received: List[str] = []

        async def on_connect(ws, request):
            while True:
                message = await ws.recv()
                if message is None:
                    return
                received.append(message)
                await ws.send_text("echo:" + message)

        server = wsproto.WebSocketServer("127.0.0.1", port, "/ws", on_connect, name="test")
        await server.start()
        try:
            connection = await wsproto.connect(f"ws://127.0.0.1:{port}/ws", timeout=3)
            ws = connection.ws
            await ws.send_text("hello")
            self.assertEqual(await asyncio.wait_for(ws.recv(), 3), "echo:hello")
            await ws.send_text("中文消息")
            self.assertEqual(await asyncio.wait_for(ws.recv(), 3), "echo:中文消息")
            await ws.close()
            self.assertTrue(await wait_until(lambda: ws.closed, 3))
        finally:
            await server.stop()
        self.assertEqual(received, ["hello", "中文消息"])


class TestGatewayWs(GatewayCase):
    async def test_flow(self):
        port = free_port()
        store = build_store(self.dir, ws_port=port, callback_port=free_port(), media_port=free_port())
        gateway = await self.start(store)
        client = await self.koishi(port)
        lifecycle = await client.event(3, "meta_event")
        self.assertEqual(lifecycle["meta_event_type"], "lifecycle")
        self.assertEqual(lifecycle["sub_type"], "connect")

        login = await client.call("get_login_info")
        self.assertEqual(login["retcode"], 0)
        self.assertEqual(login["data"]["user_id"], "wxid_mock_bot")

        status = await client.call("get_status")
        self.assertTrue(status["data"]["online"])

        group_id = "12345678@chatroom"
        await gateway.driver.inject_message("group", group_id, "早上好", sender_id="wxid_xiaoai")
        event = await client.event(3, "message")
        self.assertEqual(event["message_type"], "group")
        self.assertEqual(event["group_id"], group_id)
        self.assertEqual(event["sender"]["nickname"], "小爱")
        self.assertEqual(event["message"][0]["data"]["text"], "早上好")
        message_id = event["message_id"]

        fetched = await client.call("get_msg", {"message_id": message_id})
        self.assertEqual(fetched["data"]["message_id"], message_id)

        sent = await client.call(
            "send_group_msg",
            {"group_id": group_id, "message": [{"type": "text", "data": {"text": "大家好"}}]},
        )
        self.assertEqual(sent["retcode"], 0)
        self.assertGreater(sent["data"]["message_id"], 0)
        self_sent = await client.event(3, "message_sent")
        self.assertEqual(self_sent["group_id"], group_id)
        self.assertEqual(self_sent["message"][0]["data"]["text"], "大家好")

        groups = await client.call("get_group_list")
        self.assertEqual(groups["data"][0]["group_name"], "微信测试群")

        members = await client.call("get_group_member_list", {"group_id": group_id})
        self.assertGreaterEqual(len(members["data"]), 2)

        friend = await client.call("get_stranger_info", {"user_id": "wxid_xiaoai"})
        self.assertEqual(friend["data"]["nickname"], "小爱")

        deleted = await client.call("delete_msg", {"message_id": message_id})
        self.assertEqual(deleted["retcode"], 0)
        notice = await client.event(3, "notice")
        self.assertEqual(notice["notice_type"], "group_recall")
        self.assertEqual(notice["message_id"], message_id)

        unsupported = await client.call("no_such_action")
        self.assertEqual(unsupported["retcode"], 1404)
        self.assertEqual(unsupported["status"], "failed")

    async def test_token_rejected(self):
        port = free_port()
        store = build_store(self.dir, ws_port=port, callback_port=free_port())
        await self.start(store)
        with self.assertRaises(wsproto.WebSocketError):
            await wsproto.connect(f"ws://127.0.0.1:{port}/onebot", headers={"Authorization": "Bearer wrong"}, timeout=3)


class TestHttpApi(GatewayCase):
    async def test_api_and_push(self):
        receiver_port = free_port()
        received: List[Dict[str, Any]] = []

        async def on_push(request: RawRequest):
            received.append(request.json())
            return json_response({"ok": True})

        receiver = HttpServer("127.0.0.1", receiver_port, name="receiver")
        receiver.add_route("POST", "/push", on_push)
        await receiver.start()
        try:
            api_port = free_port()
            store = build_store(
                self.dir,
                ws_port=free_port(),
                http_port=api_port,
                push_url=f"http://127.0.0.1:{receiver_port}/push",
                callback_port=free_port(),
            )
            gateway = await self.start(store)

            status, payload, _ = await http_json_async(
                "POST",
                f"http://127.0.0.1:{api_port}/send_group_msg",
                {"group_id": "12345678@chatroom", "message": [{"type": "text", "data": {"text": "HTTP API"}}]},
                headers={"Authorization": "Bearer http-token"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["retcode"], 0)

            status, payload, _ = await http_json_async(
                "POST",
                f"http://127.0.0.1:{api_port}/get_login_info",
                {},
                headers={"Authorization": "Bearer wrong"},
            )
            self.assertEqual(status, 401)

            await gateway.driver.inject_message("private", "wxid_xiaoai", "推送给 HTTP 客户端")
            self.assertTrue(
                await wait_until(lambda: any(item.get("post_type") == "message" for item in received), 4),
                "没有收到 HTTP 事件上报",
            )
            message_events = [item for item in received if item.get("post_type") == "message"]
            self.assertEqual(message_events[0]["message"][0]["data"]["text"], "推送给 HTTP 客户端")
        finally:
            await receiver.stop()


class TestReverseWs(GatewayCase):
    async def test_reverse_connect(self):
        port = free_port()
        captured: Dict[str, Any] = {}
        lifecycle: List[Dict[str, Any]] = []

        async def on_connect(ws, request):
            captured["headers"] = dict(request.headers)
            await ws.send_text(json.dumps({"action": "get_login_info", "params": {}, "echo": "r1"}))
            while True:
                message = await ws.recv()
                if message is None:
                    return
                payload = json.loads(message)
                if "post_type" in payload:
                    lifecycle.append(payload)
                elif payload.get("echo") == "r1":
                    captured["login"] = payload
                    return

        server = wsproto.WebSocketServer("127.0.0.1", port, "/onebot", on_connect, name="koishi")
        await server.start()
        try:
            store = build_store(
                self.dir,
                ws_port=free_port(),
                callback_port=free_port(),
                reverse_url=f"ws://127.0.0.1:{port}/onebot",
            )
            await self.start(store)
            self.assertTrue(await wait_until(lambda: "login" in captured, 5), "反向 WS 未完成握手/登录")
            self.assertEqual(captured["headers"].get("x-self-id"), "wxid_mock_bot")
            self.assertEqual(captured["headers"].get("x-client-role"), "Universal")
            self.assertEqual(captured["headers"].get("authorization"), "Token rev-token")
            self.assertEqual(captured["login"]["retcode"], 0)
            self.assertEqual(lifecycle[0]["meta_event_type"], "lifecycle")
        finally:
            await server.stop()


class TestWebUI(GatewayCase):
    async def test_login_status_mock(self):
        webui_port = free_port()
        store = build_store(self.dir, ws_port=free_port(), callback_port=free_port(), webui_port=webui_port)
        await self.start(store)
        base = f"http://127.0.0.1:{webui_port}"

        status, payload, _ = await http_json_async("GET", base + "/api/status")
        self.assertEqual(status, 401)

        status, payload, headers = await http_json_async("POST", base + "/api/login", {"token": "web-token"})
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        self.assertIn("wo_token=", cookie)

        status, payload, _ = await http_json_async("GET", base + "/api/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(payload["online"])
        self.assertEqual(payload["driver"], "mock")

        status, payload, _ = await http_json_async("GET", base + "/api/mock/state", cookie=cookie)
        self.assertTrue(payload["available"])
        self.assertTrue(payload["groups"])

        status, payload, _ = await http_json_async(
            "POST",
            base + "/api/mock/message",
            {"chat_type": "group", "chat_id": "12345678@chatroom", "text": "来自 WebUI"},
            cookie=cookie,
        )
        self.assertEqual(status, 200)

        found = False
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not found:
            _, data, _ = await http_json_async("GET", base + "/api/events/recent", cookie=cookie)
            found = any((event.get("text") or "") == "来自 WebUI" for event in (data or {}).get("events", []))
            if not found:
                await asyncio.sleep(0.05)
        self.assertTrue(found, "WebUI 事件流里没有出现模拟消息")


class StubIpadService:
    """本地假的 iPad 协议服务，用于验证驱动。"""

    def __init__(self, port: int, legacy: bool = False) -> None:
        self.port = port
        self.legacy = legacy
        self.requests: List[Tuple[str, Dict[str, Any]]] = []
        self.server: Optional[asyncio.AbstractServer] = None
        self.qr_checks = 0
        self.qr_png = b"\x89PNG\r\n\x1a\nfake-qr"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        try:
            request = await read_request(reader)
            if request is None:
                return
            body = request.json() if request.body else {}
            self.requests.append((request.path, body))
            data = self._respond(request.path, body, request)
            code = 1 if self.legacy else 0
            payload = json.dumps({"Success": True, "Code": code, "Message": "成功", "Data": data}, ensure_ascii=False).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()

    def _respond(self, path: str, body: Dict[str, Any], request: Optional[RawRequest] = None) -> Any:
        if path.endswith("/Login/LoginGetQR"):
            if self.legacy:
                return {
                    "Uuid": "uuid-legacy",
                    "QrBase64": "data:image/jpg;base64," + base64.b64encode(self.qr_png).decode(),
                    "QrUrl": "https://example.com/legacy-qr",
                    "ExpiredTime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 120)),
                }
            return {
                "Uuid": "uuid-test",
                "QRCodeBase64": base64.b64encode(self.qr_png).decode(),
                "QRCodeURL": "https://example.com/qr",
                "ExpiredTime": "120",
            }
        if path.endswith("/Login/LoginCheckQR"):
            self.qr_checks += 1
            if self.legacy:
                if self.qr_checks < 2:
                    return {"uuid": "uuid-legacy", "status": 0, "expiredTime": 110}
                return {
                    "uuid": "uuid-legacy",
                    "status": 2,
                    "acctSectResp": {"userName": "wxid_legacy", "nickName": "旧版号", "fsurl": ""},
                }
            if self.qr_checks < 2:
                return {"status": 1}
            return {
                "status": 2,
                "acctSectResp": {"userName": "wxid_new", "nickName": "扫码号", "fsurl": ""},
            }
        if path.endswith("/Login/HeartBeat") or path.endswith("/Login/LoginTwiceAutoAuth") or path.endswith("/Login/LogOut"):
            return {}
        if path.endswith("/User/GetContractProfile"):
            wxid = (request.query_one("wxid") if request else "") or body.get("Wxid") or "wxid_self"
            return {
                "userInfo": {"UserName": {"string": wxid}, "NickName": {"string": "iPad 测试号"}},
                "userInfoExt": {"BigHeadImgUrl": "https://example.com/avatar.png"},
            }
        if path.endswith("/Login/HeartBeat"):
            return {}
        if path.endswith("/Friend/GetContractList"):
            return {"ContactUsernameList": ["wxid_xiaoai", "wxid_abobo", "99887766@chatroom"]}
        if path.endswith("/Friend/GetContractDetail"):
            requested = str(body.get("Towxids") or "").split(",")
            contacts = []
            for username in requested:
                if username.endswith("@chatroom"):
                    contacts.append(
                        {"UserName": username, "NickName": "iPad 测试群", "ChatRoomOwner": "wxid_xiaoai", "ContactType": 2}
                    )
                else:
                    contacts.append(
                        {"UserName": username, "NickName": "小爱" if username == "wxid_xiaoai" else "阿波", "Remark": ""}
                    )
            return {"ContactList": contacts}
        if path.endswith("/Group/GetChatRoomMemberDetail"):
            return {
                "NewChatroomData": {
                    "ChatRoomMember": [
                        {"UserName": "wxid_xiaoai", "NickName": "小爱", "ChatroomMemberFlag": 1},
                        {"UserName": "wxid_abobo", "NickName": "阿波", "ChatroomMemberFlag": 0},
                        {"UserName": "wxid_self", "NickName": "iPad 测试号", "ChatroomMemberFlag": 2},
                    ]
                }
            }
        if path.endswith("/Msg/SendTxt"):
            return {"List": [{"NewMsgId": 424242, "MsgId": 1}]}
        if path.endswith("/Msg/UploadImg"):
            return {"Newmsgid": 434343}
        if path.endswith("/Msg/Revoke"):
            return {}
        return {}


def fetch_bytes(url: str) -> Tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


class TestIpadDriver(GatewayCase):
    async def test_callback_and_send(self):
        ipad_port = free_port()
        callback_port = free_port()
        media_port = free_port()
        stub = StubIpadService(ipad_port)
        await stub.start()
        try:
            store = build_store(
                self.dir,
                driver="ipad",
                ws_port=free_port(),
                callback_port=callback_port,
                media_port=media_port,
                ipad={
                    "base_url": f"http://127.0.0.1:{ipad_port}",
                    "wxid": "wxid_self",
                    "timeout": 5,
                },
            )
            await self.start(store)
            ws_port = store.raw("onebot11")["network"]["websocketServers"][0]["port"]
            client = await self.koishi(ws_port)
            await client.event(3, "meta_event")

            login = await client.call("get_login_info")
            self.assertEqual(login["data"]["user_id"], "wxid_self")
            self.assertEqual(login["data"]["nickname"], "iPad 测试号")

            groups = await client.call("get_group_list")
            self.assertEqual(groups["data"][0]["group_name"], "iPad 测试群")

            members = await client.call("get_group_member_list", {"group_id": "99887766@chatroom"})
            roles = {m["user_id"]: m["role"] for m in members["data"]}
            self.assertEqual(roles["wxid_xiaoai"], "owner")
            self.assertEqual(roles["wxid_self"], "admin")

            image_bytes = b"\x89PNG\r\n\x1a\nfake-image"
            callback = {
                "Data": {
                    "AddMsgs": [
                        {
                            "MsgId": 1,
                            "NewMsgId": 1001,
                            "MsgType": 1,
                            "FromUserName": {"string": "wxid_xiaoai"},
                            "ToUserName": {"string": "99887766@chatroom"},
                            "Content": {"string": "wxid_xiaoai:\n@阿波 你好"},
                            "CreateTime": 1700000000,
                            "MsgSource": "<msgsource><atuserlist><![CDATA[wxid_abobo]]></atuserlist></msgsource>",
                        },
                        {
                            "MsgId": 2,
                            "NewMsgId": 1002,
                            "MsgType": 3,
                            "FromUserName": {"string": "wxid_abobo"},
                            "ToUserName": {"string": "99887766@chatroom"},
                            "Content": {"string": "<msg><img md5=\"abcdef\"/></msg>"},
                            "CreateTime": 1700000001,
                            "ImgBuf": {
                                "iLen": len(image_bytes),
                                "buffer": base64.b64encode(image_bytes).decode(),
                            },
                        },
                    ]
                }
            }
            status, _, _ = await asyncio.to_thread(
                http_json,
                "POST",
                f"http://127.0.0.1:{callback_port}/api/v1/wechat-client/wxid_self/sync-message",
                callback,
            )
            self.assertEqual(status, 200)

            text_event = await client.event(3, "message")
            self.assertEqual(text_event["message"][0]["type"], "at")
            self.assertEqual(text_event["message"][0]["data"]["qq"], "wxid_abobo")
            self.assertEqual(text_event["message"][1]["data"]["text"], "你好")

            image_event = await client.event(3, "message")
            self.assertEqual(image_event["message"][0]["type"], "image")
            image_url = image_event["message"][0]["data"]["file"]
            self.assertTrue(image_url.startswith("http://127.0.0.1:"))
            status, body = await asyncio.to_thread(fetch_bytes, image_url)
            self.assertEqual(status, 200)
            self.assertEqual(body, image_bytes)

            sent = await client.call(
                "send_group_msg",
                {"group_id": "99887766@chatroom", "message": [{"type": "text", "data": {"text": "iPad 发送"}}]},
            )
            self.assertEqual(sent["retcode"], 0)
            self.assertEqual(sent["data"]["platform_message_id"], "424242")
            send_txt = [body for path, body in stub.requests if path.endswith("/Msg/SendTxt")]
            self.assertTrue(send_txt)
            self.assertEqual(send_txt[-1]["Content"], "iPad 发送")
            self.assertEqual(send_txt[-1]["ToWxid"], "99887766@chatroom")
        finally:
            await stub.stop()


class TestBridgeDriver(GatewayCase):
    async def test_event_and_action_queue(self):
        ws_port = free_port()
        callback_port = free_port()
        store = build_store(
            self.dir,
            driver="bridge",
            ws_port=ws_port,
            callback_port=callback_port,
            bridge={"account": {"wxid": "wxid_bridge", "nickname": "桥账号"}},
        )
        gateway = await self.start(store)
        client = await self.koishi(ws_port)
        await client.event(3, "meta_event")

        def push_event() -> int:
            return http_json(
                "POST",
                f"http://127.0.0.1:{callback_port}/wechat/event",
                {
                    "type": "message",
                    "chat_type": "group",
                    "chat_id": "778899@chatroom",
                    "nickname": "阿波",
                    "user_id": "wxid_abobo",
                    "text": "桥接消息",
                },
            )[0]

        self.assertEqual(await asyncio.to_thread(push_event), 200)
        event = await client.event(3, "message")
        self.assertEqual(event["message"][0]["data"]["text"], "桥接消息")

        async def send() -> Dict[str, Any]:
            return await client.call(
                "send_group_msg",
                {"group_id": "778899@chatroom", "message": [{"type": "text", "data": {"text": "桥接发送"}}]},
            )

        send_task = asyncio.create_task(send())

        def poll() -> Optional[Dict[str, Any]]:
            _, data, _ = http_json("GET", f"http://127.0.0.1:{callback_port}/wechat/actions?wait=2")
            tasks = (data or {}).get("tasks") or []
            return tasks[0] if tasks else None

        task = await asyncio.to_thread(poll)
        self.assertIsNotNone(task, "没有取到发送任务")
        self.assertEqual(task["action"], "send_message")
        self.assertEqual(task["chat_id"], "778899@chatroom")

        def report() -> int:
            return http_json(
                "POST",
                f"http://127.0.0.1:{callback_port}/wechat/actions/result",
                {"id": task["id"], "ok": True, "platform_id": "platform-1"},
            )[0]

        self.assertEqual(await asyncio.to_thread(report), 200)
        result = await asyncio.wait_for(send_task, 5)
        self.assertEqual(result["retcode"], 0)
        self.assertEqual(result["data"]["platform_message_id"], "platform-1")



class TestWechatLogin(GatewayCase):
    async def test_qr_login_and_logout(self):
        ipad_port = free_port()
        webui_port = free_port()
        stub = StubIpadService(ipad_port)
        await stub.start()
        try:
            store = build_store(
                self.dir,
                driver="ipad",
                ws_port=free_port(),
                callback_port=free_port(),
                webui_port=webui_port,
                ipad={"base_url": f"http://127.0.0.1:{ipad_port}", "wxid": ""},
            )
            gateway = await self.start(store)
            base = f"http://127.0.0.1:{webui_port}"

            status, _, headers = await http_json_async("POST", base + "/api/login", {"token": "web-token"})
            self.assertEqual(status, 200)
            cookie = headers.get("Set-Cookie", "").split(";")[0]

            status, payload, _ = await http_json_async("GET", base + "/api/login/state", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertTrue(payload["state"]["supported"])
            self.assertFalse(payload["state"]["logged_in"])

            status, payload, _ = await http_json_async("POST", base + "/api/login/qr", {}, cookie=cookie)
            self.assertEqual(status, 200)
            qr = payload["qr"]
            self.assertEqual(qr["uuid"], "uuid-test")
            self.assertTrue(qr["image"].startswith("data:image/png;base64,"))

            result = None
            for _ in range(4):
                status, payload, _ = await http_json_async(
                    "GET", base + "/api/login/check?uuid=uuid-test", cookie=cookie
                )
                result = payload["result"]
                if payload["state"]["logged_in"]:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(result["stage"], "success")
            self.assertTrue(payload["state"]["logged_in"])
            self.assertEqual(payload["state"]["account"]["user_id"], "wxid_new")
            self.assertEqual(gateway.self_id, "wxid_new")

            status, payload, _ = await http_json_async("POST", base + "/api/login/logout", {}, cookie=cookie)
            self.assertEqual(status, 200)
            self.assertFalse(payload["state"]["logged_in"])
        finally:
            await stub.stop()



class TestLegacyIpad(GatewayCase):
    async def test_legacy_v311_qr_and_login(self):
        ipad_port = free_port()
        webui_port = free_port()
        stub = StubIpadService(ipad_port, legacy=True)
        await stub.start()
        try:
            store = build_store(
                self.dir,
                driver="ipad",
                ws_port=free_port(),
                callback_port=free_port(),
                webui_port=webui_port,
                ipad={"base_url": f"http://127.0.0.1:{ipad_port}", "wxid": ""},
            )
            gateway = await self.start(store)
            base = f"http://127.0.0.1:{webui_port}"
            _, _, headers = await http_json_async("POST", base + "/api/login", {"token": "web-token"})
            cookie = headers.get("Set-Cookie", "").split(";")[0]

            status, payload, _ = await http_json_async("POST", base + "/api/login/qr", {}, cookie=cookie)
            self.assertEqual(status, 200)
            qr = payload["qr"]
            self.assertEqual(qr["uuid"], "uuid-legacy")
            self.assertTrue(qr["image"].startswith("data:image/jpg;base64,"))
            self.assertEqual(payload["state"]["protocol"], "legacy")
            self.assertGreater(qr["expires_at"], time.time() + 60)

            result = None
            for _ in range(4):
                status, payload, _ = await http_json_async(
                    "GET", base + "/api/login/check?uuid=uuid-legacy", cookie=cookie
                )
                result = payload["result"]
                if payload["state"]["logged_in"]:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(result["stage"], "success")
            self.assertEqual(payload["state"]["account"]["user_id"], "wxid_legacy")
            self.assertEqual(gateway.driver.protocol_flavor, "legacy")
        finally:
            await stub.stop()


class TestPreflight(unittest.IsolatedAsyncioTestCase):
    async def test_busy_port_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            busy_port = sock.getsockname()[1]
            try:
                store = build_store(
                    Path(tmp),
                    ws_port=busy_port,
                    callback_port=free_port(),
                )
                problems = preflight_check(store)
                self.assertTrue(any(str(busy_port) in item for item in problems), problems)
            finally:
                sock.close()


class TestStartupFailFast(GatewayCase):
    async def test_webui_bind_failure_raises(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        busy_port = sock.getsockname()[1]
        try:
            store = build_store(
                self.dir,
                ws_port=free_port(),
                callback_port=free_port(),
                webui_port=busy_port,
            )
            gateway = Gateway(store)
            with self.assertRaises(StartupError):
                await gateway.start()
            await gateway.stop()
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
