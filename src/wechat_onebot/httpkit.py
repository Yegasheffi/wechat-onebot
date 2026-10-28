"""HTTP 基础：请求解析、响应写出、SSE 流。足够小，但比 `http.server` 更适合 asyncio。"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

STATUS_REASONS = {
    101: "Switching Protocols",
    200: "OK",
    204: "No Content",
    301: "Moved Permanently",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or STATUS_REASONS.get(status, "error"))
        self.status = status
        self.message = message or STATUS_REASONS.get(status, "error")


@dataclass
class RawRequest:
    method: str
    target: str
    version: str
    headers: Dict[str, str]
    body: bytes
    path: str = ""
    query: Dict[str, List[str]] = field(default_factory=dict)
    params: Dict[str, str] = field(default_factory=dict)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def query_one(self, name: str, default: str = "") -> str:
        values = self.query.get(name) or []
        return values[0] if values else default

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, f"JSON 解析失败: {exc}") from exc

    def text(self) -> str:
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError:
            return self.body.decode("utf-8", "replace")


def _parse_target(target: str) -> Tuple[str, Dict[str, List[str]]]:
    split = urllib.parse.urlsplit(target)
    path = urllib.parse.unquote(split.path or "/")
    query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
    return path, query


async def _read_until_headers(reader) -> Tuple[bytes, bytes]:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = await reader.read(4096)
        if not chunk:
            return b"", b""
        buffer += chunk
        if len(buffer) > 128 * 1024:
            raise HttpError(431, "请求头过大")
    head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    return head, rest


async def _read_body(reader, headers: Dict[str, str], rest: bytes) -> bytes:
    transfer = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer:
        body = bytearray(rest)
        decoded = bytearray()
        while True:
            while b"\r\n" not in body:
                chunk = await reader.read(4096)
                if not chunk:
                    raise HttpError(400, "chunked 请求体不完整")
                body += chunk
            line, _, tail = bytes(body).partition(b"\r\n")
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError as exc:
                raise HttpError(400, "chunked 长度非法") from exc
            body = bytearray(tail)
            if size == 0:
                # 读取 trailer 结束的空行
                while b"\r\n" not in body:
                    chunk = await reader.read(1024)
                    if not chunk:
                        break
                    body += chunk
                return bytes(decoded)
            need = size + 2
            while len(body) < need:
                chunk = await reader.read(4096)
                if not chunk:
                    raise HttpError(400, "chunked 请求体不完整")
                body += chunk
            decoded += body[:size]
            body = bytearray(body[need:])
    length_header = headers.get("content-length", "").strip()
    if not length_header:
        return bytes(rest)
    try:
        length = int(length_header)
    except ValueError as exc:
        raise HttpError(400, "Content-Length 非法") from exc
    if length <= 0:
        return b""
    if length > 64 * 1024 * 1024:
        raise HttpError(413, "请求体过大")
    body = bytearray(rest)
    while len(body) < length:
        chunk = await reader.read(min(65536, length - len(body)))
        if not chunk:
            raise HttpError(400, "请求体不完整")
        body += chunk
    return bytes(body[:length])


async def read_request(reader) -> Optional[RawRequest]:
    head, rest = await _read_until_headers(reader)
    if not head:
        return None
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, version = lines[0].split(" ", 2)
    except ValueError as exc:
        raise HttpError(400, "请求行非法") from exc
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    body = await _read_body(reader, headers, rest)
    path, query = _parse_target(target)
    return RawRequest(method.upper(), target, version, headers, body, path, query)


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json; charset=utf-8"

    def with_header(self, name: str, value: str) -> "Response":
        self.headers[name] = value
        return self


def json_response(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return Response(status=status, body=body)


def text_response(text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(status=status, body=text.encode("utf-8"), content_type=content_type)


async def write_response(writer, response: Response) -> None:
    reason = STATUS_REASONS.get(response.status, "OK")
    lines = [f"HTTP/1.1 {response.status} {reason}"]
    headers = {"Content-Type": response.content_type, "Content-Length": str(len(response.body))}
    headers.update(response.headers)
    headers.setdefault("Connection", "close")
    headers.setdefault("Server", "wechat-onebot")
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    if response.body:
        writer.write(response.body)
    await writer.drain()


async def write_stream_headers(writer, status: int, headers: Dict[str, str]) -> None:
    reason = STATUS_REASONS.get(status, "OK")
    lines = [f"HTTP/1.1 {status} {reason}"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append("")
    writer.write("\r\n".join(lines).encode("latin-1"))
    await writer.drain()


class SSEStream:
    """供 WebUI 使用的 Server-Sent Events 流。"""

    def __init__(self, writer, status: int = 200, headers: Optional[Dict[str, str]] = None) -> None:
        self.writer = writer
        self.status = status
        self.headers = headers or {}
        self._opened = False

    async def open(self) -> None:
        headers = {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        }
        headers.update(self.headers)
        await write_stream_headers(self.writer, self.status, headers)
        self._opened = True

    async def send(self, event: str, data: Any) -> bool:
        if not self._opened:
            await self.open()
        if isinstance(data, (dict, list)):
            payload = json.dumps(data, ensure_ascii=False)
        else:
            payload = str(data)
        payload = payload.replace("\r\n", "\n")
        chunk = f"event: {event}\n"
        for line in payload.split("\n"):
            chunk += f"data: {line}\n"
        chunk += "\n"
        try:
            self.writer.write(chunk.encode("utf-8"))
            await self.writer.drain()
            return True
        except (ConnectionError, RuntimeError):
            return False

    async def comment(self, text: str = "ping") -> bool:
        if not self._opened:
            await self.open()
        try:
            self.writer.write(f": {text}\n\n".encode("utf-8"))
            await self.writer.drain()
            return True
        except (ConnectionError, RuntimeError):
            return False


Handler = Callable[[RawRequest, Any], Awaitable[Any]]
