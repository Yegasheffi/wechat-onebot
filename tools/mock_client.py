"""模拟 OneBot 11 客户端。

用来在不连接真实客户端或框架的情况下自检本网关：
    python3 tools/mock_client.py --url ws://127.0.0.1:3001/onebot --token 你的token --demo
    python3 tools/mock_client.py --url ws://127.0.0.1:3001/onebot          # 交互模式
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_onebot import wsproto  # noqa: E402


class MockClient:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token
        self.ws: Optional[wsproto.WebSocket] = None
        self.counter = 0
        self.events: list[dict] = []
        self._responses: Dict[str, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        headers = {"User-Agent": "mock-onebot/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        connection = await wsproto.connect(self.url, headers=headers, timeout=8)
        self.ws = connection.ws
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.ws is not None
        while True:
            try:
                message = await self.ws.recv()
            except wsproto.WebSocketError as exc:
                print(f"  ! {exc}")
                return
            if message is None:
                print("  连接已断开")
                return
            payload = json.loads(message)
            if "post_type" in payload:
                self._handle_event(payload)
            elif payload.get("echo") in self._responses:
                future = self._responses.pop(payload["echo"])
                if not future.done():
                    future.set_result(payload)

    def _handle_event(self, payload: dict) -> None:
        self.events.append(payload)
        post_type = payload.get("post_type")
        if post_type == "message":
            segments = payload.get("message") or []
            text = "".join(
                seg.get("data", {}).get("text", "") for seg in segments if isinstance(seg, dict)
            ) or "[富媒体]"
            sender = payload.get("sender") or {}
            name = sender.get("card") or sender.get("nickname") or payload.get("user_id")
            where = payload.get("group_id") or "私聊 " + str(payload.get("user_id"))
            print(f"  [消息] {where} | {name}: {text}")
        elif post_type == "message_sent":
            print(f"  [自身消息] -> {payload.get('target_id') or payload.get('group_id')}")
        elif post_type == "notice":
            print(f"  [通知] {payload.get('notice_type')} {payload.get('sub_type') or ''}")
        elif post_type == "request":
            print(f"  [请求] {payload.get('request_type')} {payload.get('sub_type') or ''}")
        elif post_type == "meta_event" and payload.get("meta_event_type") == "lifecycle":
            print(f"  [连接] lifecycle/{payload.get('sub_type')} self_id={payload.get('self_id')}")

    async def call(self, action: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        if self.ws is None:
            raise RuntimeError("尚未连接")
        self.counter += 1
        echo = f"mock-{self.counter}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._responses[echo] = future
        await self.ws.send_text(json.dumps({"action": action, "params": params or {}, "echo": echo}))
        return await asyncio.wait_for(future, timeout)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()


async def demo(client: MockClient) -> None:
    await asyncio.sleep(0.3)
    login = await client.call("get_login_info")
    print("  get_login_info ->", json.dumps(login.get("data"), ensure_ascii=False))

    status = await client.call("get_status")
    data = status.get("data") or {}
    print(f"  get_status -> online={data.get('online')} good={data.get('good')}")

    groups = (await client.call("get_group_list")).get("data") or []
    print(f"  get_group_list -> {len(groups)} 个群：{[g.get('group_name') for g in groups]}")
    if groups:
        group_id = groups[0]["group_id"]
        members = (await client.call("get_group_member_list", {"group_id": group_id})).get("data") or []
        print(f"  get_group_member_list -> {[m.get('nickname') for m in members]}")
        result = await client.call(
            "send_group_msg",
            {
                "group_id": group_id,
                "message": [{"type": "text", "data": {"text": "来自模拟客户端的自检消息"}}],
            },
        )
        print("  send_group_msg ->", json.dumps(result.get("data"), ensure_ascii=False))

    friends = (await client.call("get_friend_list")).get("data") or []
    print(f"  get_friend_list -> {len(friends)} 个好友：{[f.get('nickname') for f in friends[:5]]}")
    await asyncio.sleep(0.4)


async def interactive(client: MockClient) -> None:
    print("\n  命令：")
    print("    api <action> [json]           调用 OneBot API")
    print("    send <group|private> <id> <文本>")
    print("    quit")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (RuntimeError, ValueError):
            return
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            return
        try:
            if line.startswith("api "):
                parts = line[4:].split(" ", 1)
                action = parts[0]
                params = json.loads(parts[1]) if len(parts) > 1 else {}
                result = await client.call(action, params)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif line.startswith("send "):
                _, chat_type, chat_id, text = line.split(" ", 3)
                action = "send_group_msg" if chat_type == "group" else "send_private_msg"
                key = "group_id" if chat_type == "group" else "user_id"
                result = await client.call(
                    action, {key: chat_id, "message": [{"type": "text", "data": {"text": text}}]}
                )
                print(json.dumps(result.get("data"), ensure_ascii=False))
            else:
                print("  未知命令：api / send / quit")
        except Exception as exc:
            print(f"  ! {exc}")


async def main_async(args) -> int:
    client = MockClient(args.url, args.token)
    try:
        await client.connect()
    except Exception as exc:
        print(f"连接失败：{exc}")
        print("提示：先启动 python3 -m wechat_onebot，并确认端口与 token 一致")
        return 1
    print(f"已连接 {args.url}")
    try:
        if args.demo:
            await demo(client)
        else:
            await interactive(client)
    finally:
        await client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟 OneBot 11 客户端（用于自检，不连接真实客户端）")
    parser.add_argument("--url", default="ws://127.0.0.1:3001/onebot", help="本网关的正向 WS 地址")
    parser.add_argument("--token", default="", help="与 onebot11.json 中 token 一致")
    parser.add_argument("--demo", action="store_true", help="跑一遍自检流程后退出")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
