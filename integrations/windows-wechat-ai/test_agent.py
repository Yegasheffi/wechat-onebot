#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 macOS/Linux 上验证 Windows 代理的桥接协议（不需要微信/Windows）。"""

from __future__ import annotations

import json
import sys
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_agent import AgentConfig, BridgeAgent, MockWeChat, segments_to_text  # noqa: E402


class FakeGateway(BaseHTTPRequestHandler):
    events: list[dict] = []
    results: list[dict] = []
    actions_sent = 0
    lock = threading.Lock()

    def log_message(self, *args):  # noqa: D401
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/wechat/event"):
            with self.lock:
                self.events.append(body)
            self._json({"ok": True})
        elif self.path.startswith("/wechat/actions/result"):
            with self.lock:
                self.results.append(body)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_GET(self):
        if self.path.startswith("/wechat/actions"):
            with self.lock:
                first = self.actions_sent == 0
                if first:
                    self.actions_sent += 1
            if first:
                self._json(
                    {
                        "tasks": [
                            {
                                "id": 1,
                                "action": "send_message",
                                "chat_type": "group",
                                "chat_id": "123456@chatroom",
                                "segments": [{"type": "text", "data": {"text": "你好，我是 AI"}}],
                            }
                        ]
                    }
                )
            else:
                time.sleep(0.2)
                self._json({"tasks": []})
        elif self.path.startswith("/wechat/state"):
            self._json({"account": {"user_id": "wxid_mock_bot", "nickname": "测试机器人"}})
        else:
            self._json({"error": "not found"}, 404)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_bind(self):
            # HTTPServer.server_bind 会做一次反向 DNS（getfqdn），在 macOS 上可能卡很久
            socketserver.TCPServer.server_bind(self)
            self.server_name = "127.0.0.1"
            self.server_port = self.server_address[1]

    server = Server(("127.0.0.1", 0), FakeGateway)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config = AgentConfig(
        gateway=f"http://127.0.0.1:{port}",
        token="test-token",
        poll_wait=1,
        request_timeout=3,
    )
    wechat = MockWeChat()
    agent = BridgeAgent(config, wechat)
    agent.start()
    try:
        wechat.emit(
            "123456@chatroom",
            "wxid_alice:\n@机器人 你好",
            sender_id="wxid_alice",
            sender_name="小爱",
        )
        assert wait_for(lambda: any(e.get("type") == "message" for e in FakeGateway.events)), "没有收到消息事件"
        assert wait_for(lambda: len(wechat.sent) >= 1), "没有执行发送任务"
        assert wait_for(lambda: any(r.get("id") == 1 for r in FakeGateway.results)), "没有回传任务结果"

        event = next(e for e in FakeGateway.events if e.get("type") == "message")
        assert event["chat_type"] == "group", event
        assert event["chat_id"] == "123456@chatroom", event
        assert event["segments"][0]["data"]["text"].endswith("你好"), event
        assert wechat.sent[0] == ("123456@chatroom", "你好，我是 AI"), wechat.sent
        assert FakeGateway.results[0]["ok"] is True, FakeGateway.results

        assert segments_to_text([{"type": "text", "data": {"text": "abc"}}]) == "abc"
        print("PASS：Windows 代理 → bridge 事件 / 拉取任务 / 微信发送 / 回传结果")
        return 0
    finally:
        agent.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
