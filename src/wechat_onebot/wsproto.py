"""最小但完整的 WebSocket（RFC 6455）实现：服务端 + 客户端，零第三方依赖。

只依赖 asyncio，够 OneBot 用：
  * 文本帧 / 二进制帧 / 分片重组
  * ping / pong / close 控制帧
  * 客户端掩码、长度 7/16/64 位
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
import urllib.parse
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Tuple

from .httpkit import HttpError, RawRequest, read_request
from .logger import get_logger

log = get_logger("ws")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_MESSAGE = 32 * 1024 * 1024
MAX_FRAME = 16 * 1024 * 1024


class WebSocketError(Exception):
    pass


class ConnectionClosed(WebSocketError):
    def __init__(self, code: int = 1006, reason: str = "") -> None:
        super().__init__(f"连接已关闭 ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


def _mask(payload: bytes, key: bytes) -> bytes:
    if not payload:
        return b""
    length = len(payload)
    repeated = (key * (length // 4 + 1))[:length]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(length, "big")


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_key(client_key: str) -> bool:
    try:
        return len(base64.b64decode(client_key)) == 16
    except Exception:
        return False


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, is_client: bool) -> None:
        self.reader = reader
        self.writer = writer
        self.is_client = is_client
        self.closed = False
        self.close_code: Optional[int] = None
        self.close_reason: str = ""
        self._send_lock = asyncio.Lock()
        self._reading = False
        peername = writer.get_extra_info("peername")
        self.remote = f"{peername[0]}:{peername[1]}" if peername else "?"

    # -- 发送 --------------------------------------------------------------
    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise ConnectionClosed(self.close_code or 1006, "发送时连接已关闭")
        fin_bit = 0x80
        header = bytearray()
        header.append(fin_bit | (opcode & 0x0F))
        length = len(payload)
        mask_bit = 0x80 if self.is_client else 0x00
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        if self.is_client:
            key = os.urandom(4)
            header += key
            payload = _mask(payload, key)
        async with self._send_lock:
            self.writer.write(bytes(header) + payload)
            try:
                await self.writer.drain()
            except (ConnectionError, RuntimeError) as exc:
                self.closed = True
                raise ConnectionClosed(1006, str(exc)) from exc

    async def send_text(self, text: str) -> None:
        await self._send_frame(OP_TEXT, text.encode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        await self._send_frame(OP_BINARY, data)

    async def ping(self, payload: bytes = b"") -> None:
        await self._send_frame(OP_PING, payload[:125])

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
        try:
            await self._send_frame(OP_CLOSE, payload)
        except Exception:
            pass
        self.closed = True
        self.close_code = code
        self.close_reason = reason
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    # -- 接收 --------------------------------------------------------------
    async def _read_frame(self) -> Tuple[int, bool, bytes]:
        try:
            header = await self.reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            raise ConnectionClosed(1006, "连接被对端关闭") from exc
        first, second = header[0], header[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        if first & 0x70:
            raise WebSocketError("不支持扩展位 (RSV)")
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        if length > MAX_FRAME:
            raise WebSocketError(f"帧过大: {length}")
        if masked:
            mask_key = await self.reader.readexactly(4)
        else:
            mask_key = b""
        payload = await self.reader.readexactly(length) if length else b""
        if mask_key:
            payload = _mask(payload, mask_key)
        if opcode >= 0x8:
            if not fin or length > 125:
                raise WebSocketError("非法控制帧")
        return opcode, fin, payload

    async def recv(self) -> Optional[bytes | str]:
        """读取一条完整消息；连接关闭返回 None。"""
        if self._reading:
            raise WebSocketError("recv() 不能并发调用，请用单一读取循环分发消息")
        if self.closed:
            return None
        self._reading = True
        try:
            return await self._recv_loop()
        finally:
            self._reading = False

    async def _recv_loop(self) -> Optional[bytes | str]:
        fragments = bytearray()
        message_opcode: Optional[int] = None
        while True:
            try:
                opcode, fin, payload = await self._read_frame()
            except ConnectionClosed:
                self.closed = True
                return None
            if opcode == OP_PING:
                try:
                    await self._send_frame(OP_PONG, payload)
                except ConnectionClosed:
                    return None
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = 1005
                reason = ""
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                    reason = payload[2:].decode("utf-8", "replace")
                try:
                    await self._send_frame(OP_CLOSE, payload[:125])
                except Exception:
                    pass
                self.closed = True
                self.close_code = code
                self.close_reason = reason
                try:
                    self.writer.close()
                except Exception:
                    pass
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                if message_opcode is not None:
                    raise WebSocketError("上一帧尚未结束")
                message_opcode = opcode
                fragments += payload
            elif opcode == OP_CONT:
                if message_opcode is None:
                    raise WebSocketError("意外的续帧")
                fragments += payload
            else:
                raise WebSocketError(f"未知操作码: {opcode}")
            if len(fragments) > MAX_MESSAGE:
                raise WebSocketError("消息过大")
            if fin:
                if message_opcode == OP_TEXT:
                    return fragments.decode("utf-8", "replace")
                return bytes(fragments)

    async def wait_closed(self) -> None:
        while not self.closed:
            if await self.recv() is None:
                return


def _origin_headers(request: RawRequest) -> str:
    return f"{request.header('host', 'unknown')}{request.path}"


async def upgrade(writer: asyncio.StreamWriter, request: RawRequest) -> str:
    """校验并回写 101，返回 subprotocol。"""
    if request.method != "GET":
        raise HttpError(405, "WebSocket 只接受 GET")
    if request.header("upgrade").lower() != "websocket":
        raise HttpError(400, "缺少 Upgrade: websocket")
    connection = request.header("connection").lower()
    if "upgrade" not in connection:
        raise HttpError(400, "缺少 Connection: Upgrade")
    key = request.header("sec-websocket-key")
    if not key or not validate_key(key):
        raise HttpError(400, "Sec-WebSocket-Key 非法")
    version = request.header("sec-websocket-version", "13")
    if version != "13":
        raise HttpError(400, "只支持 WebSocket 13")
    accept = accept_key(key)
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept}",
        "Server: wechat-onebot",
        "",
        "",
    ]
    writer.write("\r\n".join(lines).encode("latin-1"))
    await writer.drain()
    return ""


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------

ConnectHandler = Callable[[WebSocket, RawRequest], Awaitable[None]]
VerifyHandler = Callable[[RawRequest], Optional[str]]


@dataclass
class WebSocketServer:
    host: str
    port: int
    path: str
    on_connect: ConnectHandler
    verify: Optional[VerifyHandler] = None
    name: str = "ws"
    _server: Optional[asyncio.AbstractServer] = None
    _connections: set = field(default_factory=set)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=2 ** 20)
        log.info("WebSocket 服务端 [%s] 监听 ws://%s:%s%s", self.name, self.host, self.port, self.path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for ws in list(self._connections):
            await ws.close(1001, "服务端关闭")
        self._connections.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ws: Optional[WebSocket] = None
        try:
            request = await read_request(reader)
            if request is None:
                return
            if self.path and request.path != self.path:
                await _reject(writer, 404, f"未知路径 {request.path}")
                return
            if self.verify:
                reason = self.verify(request)
                if reason:
                    await _reject(writer, 401, reason)
                    return
            await upgrade(writer, request)
            ws = WebSocket(reader, writer, is_client=False)
            self._connections.add(ws)
            log.info("WebSocket [%s] 新连接 %s", self.name, ws.remote)
            await self.on_connect(ws, request)
        except HttpError as exc:
            try:
                await _reject(writer, exc.status, exc.message)
            except Exception:
                pass
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # pragma: no cover - 防御性
            log.warning("WebSocket [%s] 连接异常: %s", self.name, exc)
        finally:
            if ws is not None:
                self._connections.discard(ws)
                ws.closed = True
            try:
                writer.close()
            except Exception:
                pass

    @property
    def connection_count(self) -> int:
        return len(self._connections)


async def _reject(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    from .httpkit import STATUS_REASONS

    reason = STATUS_REASONS.get(status, "Error")
    body = message.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "",
        "",
    ]
    writer.write("\r\n".join(lines).encode("latin-1") + body)
    await writer.drain()
    writer.close()


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class WebSocketClientConnection:
    def __init__(self, ws: WebSocket, status: int, headers: Dict[str, str]) -> None:
        self.ws = ws
        self.status = status
        self.headers = headers


async def connect(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> WebSocketClientConnection:
    """作为客户端连接 ws:// 或 wss://。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("ws", "wss"):
        raise WebSocketError(f"不支持的协议: {parsed.scheme}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    use_ssl = parsed.scheme == "wss"
    if use_ssl and ssl_context is None:
        ssl_context = ssl.create_default_context()

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_context if use_ssl else None, limit=2 ** 20),
        timeout=timeout,
    )
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    host_header = host if port in (80, 443) else f"{host}:{port}"
    request_headers = {
        "Host": host_header,
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Version": "13",
        "User-Agent": "wechat-onebot/1.0",
    }
    if headers:
        request_headers.update(headers)
    raw = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request_headers.items()) + "\r\n"
    writer.write(raw.encode("latin-1"))
    await writer.drain()

    try:
        head, leftover = await asyncio.wait_for(_read_head(reader), timeout=timeout)
    except asyncio.TimeoutError:
        writer.close()
        raise WebSocketError("握手超时")

    lines = head.decode("latin-1").split("\r\n")
    try:
        _, status_text, _ = lines[0].split(" ", 2)
        status = int(status_text)
    except ValueError as exc:
        writer.close()
        raise WebSocketError(f"握手响应非法: {lines[0]}") from exc
    response_headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        response_headers[name.strip().lower()] = value.strip()
    if status != 101:
        writer.close()
        raise WebSocketError(f"握手失败: HTTP {status} {response_headers.get('www-authenticate', '')}".strip())
    expected = accept_key(key)
    if response_headers.get("sec-websocket-accept") != expected:
        writer.close()
        raise WebSocketError("Sec-WebSocket-Accept 校验失败")
    if leftover:
        # 服务端可能把 101 和第一帧放在同一个包里
        reader.feed_data(leftover)
    return WebSocketClientConnection(WebSocket(reader, writer, is_client=True), status, response_headers)


async def _read_head(reader: asyncio.StreamReader) -> Tuple[bytes, bytes]:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = await reader.read(4096)
        if not chunk:
            raise WebSocketError("握手时连接被关闭")
        buffer += chunk
    head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    return head, rest
