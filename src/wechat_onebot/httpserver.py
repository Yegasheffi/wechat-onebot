"""极简 asyncio HTTP 服务器：路由 + 静态文件 + SSE。

用于：
  * OneBot 11 的 HTTP API / HTTP 事件上报
  * 微信驱动的回调（iPad 协议、Webhook 桥）
  * WebUI
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Pattern, Tuple

from .httpkit import (
    HttpError,
    RawRequest,
    Response,
    SSEStream,
    json_response,
    read_request,
    text_response,
    write_response,
)
from .logger import get_logger

log = get_logger("http")

Handler = Callable[[RawRequest], Awaitable[Optional[Response]]]
StreamHandler = Callable[[RawRequest, SSEStream], Awaitable[None]]


class Route:
    def __init__(self, method: str, pattern: str, handler, stream: bool = False) -> None:
        self.method = method.upper()
        self.pattern = pattern
        self.stream = stream
        self.handler = handler
        self.regex: Pattern[str] = _compile_pattern(pattern)

    def match(self, method: str, path: str) -> Optional[Dict[str, str]]:
        if self.method != "*" and self.method != method:
            return None
        match = self.regex.fullmatch(path)
        if not match:
            return None
        return match.groupdict()


class StaticRoute:
    def __init__(self, path: Path) -> None:
        self.path = path


def _compile_pattern(pattern: str) -> Pattern[str]:
    index = 0
    parts: List[str] = []
    for match in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", pattern):
        parts.append(re.escape(pattern[index : match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        index = match.end()
    parts.append(re.escape(pattern[index:]))
    return re.compile("".join(parts))


def _safe_join(root: Path, relative: str) -> Optional[Path]:
    try:
        root_resolved = root.resolve()
        candidate = (root / relative).resolve()
        if candidate == root_resolved or root_resolved in candidate.parents:
            return candidate
    except (OSError, RuntimeError):
        return None
    return None


MIME_OVERRIDES = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}


class HttpServer:
    def __init__(self, host: str, port: int, name: str = "http") -> None:
        self.host = host
        self.port = port
        self.name = name
        self.routes: List[Route] = []
        self.statics: List[Tuple[str, Path]] = []  # (prefix, directory)
        self._server: Optional[asyncio.AbstractServer] = None
        self._stream_tasks: set = set()
        self._running = False

    # -- 注册 --------------------------------------------------------------
    def add_route(self, method: str, pattern: str, handler: Handler) -> None:
        self.routes.append(Route(method, pattern, handler))

    def add_sse(self, pattern: str, handler: StreamHandler) -> None:
        self.routes.append(Route("GET", pattern, handler, stream=True))

    def add_static(self, prefix: str, directory: Path) -> None:
        self.statics.append((prefix.rstrip("/") or "/", Path(directory)))

    # -- 生命周期 ----------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=2 ** 20)
        self._running = True
        log.info("HTTP [%s] 监听 http://%s:%s", self.name, self.host, self.port)

    async def stop(self) -> None:
        self._running = False
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for task in list(self._stream_tasks):
            task.cancel()
        self._stream_tasks.clear()

    @property
    def is_running(self) -> bool:
        return self._running

    # -- 处理 --------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await read_request(reader)
            if request is None:
                return
            route, params = self._match(request.method, request.path)
            if route is None:
                await write_response(writer, text_response("Not Found", 404))
                return
            request.params = params
            if isinstance(route, StaticRoute):
                await write_response(writer, self._static_response(route, request))
                return
            if route.stream:
                stream = SSEStream(writer)
                task = asyncio.create_task(self._run_stream(route, request, stream))
                self._stream_tasks.add(task)
                task.add_done_callback(self._stream_tasks.discard)
                await task
                return
            response = await route.handler(request)
            await write_response(writer, response or text_response("Not Found", 404))
        except HttpError as exc:
            try:
                await write_response(writer, json_response({"error": exc.message}, exc.status))
            except Exception:
                pass
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            log.warning("HTTP [%s] 处理异常: %s", self.name, exc)
            try:
                await write_response(writer, json_response({"error": str(exc)}, 500))
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _run_stream(self, route: Route, request: RawRequest, stream: SSEStream) -> None:
        # handler 通过 stream.send() 的返回值感知断开，自行退出
        try:
            await route.handler(request, stream)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("SSE [%s] 结束: %s", self.name, exc)

    def _static_response(self, route: StaticRoute, request: RawRequest) -> Response:
        path = route.path
        if path.is_dir():
            path = path / "index.html"
        if not path.is_file():
            raise HttpError(404, "文件不存在")
        suffix = path.suffix.lower()
        content_type = MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if content_type.startswith("text/") and "charset" not in content_type:
            content_type += "; charset=utf-8"
        return Response(
            status=200,
            body=path.read_bytes(),
            content_type=content_type,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    def _match(self, method: str, path: str):
        for route in self.routes:
            params = route.match(method, path)
            if params is not None:
                return route, params
        if method in ("GET", "HEAD"):
            for prefix, directory in self.statics:
                if prefix == "/":
                    relative = path.lstrip("/")
                elif path.startswith(prefix + "/"):
                    relative = path[len(prefix) + 1 :]
                elif path == prefix:
                    relative = ""
                else:
                    continue
                relative = relative or "index.html"
                resolved = _safe_join(directory, relative)
                if resolved is not None and resolved.is_file():
                    return StaticRoute(resolved), {}
                resolved = _safe_join(directory, "index.html")
                if resolved is not None and resolved.is_file():
                    return StaticRoute(resolved), {}
        return None, {}
