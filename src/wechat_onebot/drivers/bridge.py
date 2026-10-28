"""通用 Webhook 桥驱动。

用途：任何微信 hook 程序（WeChatFerry / ntchat / 自研协议端）都可以通过 HTTP 接入本网关，
不必关心 OneBot。分成两条通道：

  入方向  POST /wechat/event          由 hook 程序推送消息 / 通知 / 请求
  出方向  GET  /wechat/actions?wait=25 由 hook 程序长轮询取发送任务（或配置 action_callback_url 主动推送）

入方向 JSON（推荐写法）：
  {
    "type": "message",
    "chat_type": "group",
    "chat_id": "123456@chatroom",
    "platform_id": "123456789",
    "sender": {"user_id": "wxid_x", "nickname": "小爱", "role": "member"},
    "segments": [{"type": "text", "data": {"text": "你好"}}],
    "time": 1730000000
  }
也接受简化写法：{"type":"message","chat_type":"private","chat_id":"wxid_x","nickname":"小爱","text":"你好"}

出方向任务：
  {"id": 1, "action": "send_message", "chat_type": "group", "chat_id": "...", "segments": [...]}
hook 程序处理完后 POST /wechat/actions/result  {"id": 1, "ok": true, "platform_id": "..."} 即可。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Dict, List, Optional, Tuple

from ..httpkit import HttpError, RawRequest, json_response
from ..model import (
    ChatMessage,
    DriverError,
    Friend,
    Group,
    GroupMember,
    LoginInfo,
    Notice,
    Segment,
    Sender,
)
from .base import BaseDriver


class BridgeDriver(BaseDriver):
    name = "bridge"

    def __init__(self, config: Dict[str, Any], ctx: Any) -> None:
        super().__init__(config, ctx)
        self.capabilities = {"text", "image", "record", "video", "file", "at", "reply", "revoke", "group_manage"}
        account = config.get("account") or {}
        self.self_id = str(account.get("wxid") or "bridge_account")
        self.nickname = str(account.get("nickname") or "微信桥")
        self.action_callback_url = str(config.get("action_callback_url") or "")
        self.action_timeout = float(config.get("action_timeout") or 30)
        self.token = str(config.get("token") or "")
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._pending: Dict[int, asyncio.Future] = {}
        self._counter = itertools.count(1)
        self._friends: Dict[str, Friend] = {}
        self._groups: Dict[str, Group] = {}

    async def start(self) -> None:
        await super().start()
        self.ctx.register_route("POST", "/wechat/event", self._on_event)
        self.ctx.register_route("GET", "/wechat/actions", self._on_actions)
        self.ctx.register_route("POST", "/wechat/actions/result", self._on_action_result)
        self.ctx.register_route("GET", "/wechat/state", self._on_state)
        self.log.info("Webhook 桥已就绪：POST /wechat/event 接收消息，GET /wechat/actions 拉取发送任务")

    async def login(self) -> LoginInfo:
        return LoginInfo(self.self_id, self.nickname)

    def _check_token(self, request: RawRequest) -> None:
        if not self.token:
            return
        supplied = request.header("x-token") or request.query_one("token")
        if supplied != self.token:
            raise HttpError(401, "token 校验失败")

    # -- 入方向 ------------------------------------------------------------
    async def _on_event(self, request: RawRequest):
        self._check_token(request)
        body = request.json()
        await self._ingest(body)
        return json_response({"ok": True})

    async def _ingest(self, body: Any) -> None:
        if not isinstance(body, dict):
            raise HttpError(400, "body 必须是对象")
        kind = str(body.get("type") or "message").lower()
        if kind == "message":
            await self._ingest_message(body)
        elif kind == "notice":
            await self._ingest_notice(body)
        elif kind == "request":
            await self._ingest_request(body)
        elif kind == "sync":
            await self._ingest_sync(body)
        elif kind == "account":
            wxid = str(body.get("user_id") or body.get("wxid") or "")
            if wxid:
                self.self_id = wxid
                self.nickname = str(body.get("nickname") or self.nickname)
                await self.ctx.emit_login(LoginInfo(self.self_id, self.nickname))
        else:
            raise HttpError(400, f"未知 type: {kind}")

    async def _ingest_message(self, body: Dict[str, Any]) -> None:
        chat_type = str(body.get("chat_type") or ("group" if str(body.get("group_id") or "").endswith("@chatroom") else "private"))
        chat_id = str(body.get("chat_id") or body.get("group_id") or body.get("user_id") or "")
        if not chat_id:
            raise HttpError(400, "缺少 chat_id")
        sender_raw = body.get("sender") or {}
        sender_id = str(sender_raw.get("user_id") or body.get("user_id") or "")
        sender_name = str(sender_raw.get("nickname") or body.get("nickname") or sender_id or chat_id)
        role = str(sender_raw.get("role") or body.get("role") or "member")
        if not sender_id:
            sender_id = chat_id if chat_type == "private" else "unknown"
        segments: List[Segment] = []
        if body.get("segments"):
            for item in body["segments"]:
                segments.append(Segment.from_dict(item))
        elif body.get("text"):
            segments = [Segment("text", {"text": str(body["text"])})]
        if not segments:
            raise HttpError(400, "缺少 text / segments")
        if chat_type == "group":
            group = self._groups.get(chat_id) or Group(chat_id, str(body.get("chat_name") or chat_id))
            if not group.member(sender_id):
                group.members.append(GroupMember(sender_id, sender_name, role=role))
            self._groups[chat_id] = group
        else:
            self._friends.setdefault(sender_id, Friend(sender_id, sender_name))
        message = ChatMessage(
            platform_id=str(body.get("platform_id") or f"bridge-{next(self._counter)}"),
            chat_type=chat_type,
            chat_id=chat_id,
            sender=Sender(sender_id, sender_name, "", role=role),
            segments=segments,
            time=int(body.get("time") or time.time()),
            is_self=bool(body.get("is_self", False)),
            quote_platform_id=str(body.get("quote_platform_id") or "") or None,
            raw=body,
        )
        await self.ctx.emit_message(message)

    async def _ingest_notice(self, body: Dict[str, Any]) -> None:
        notice = Notice(
            kind=str(body.get("notice_type") or "group_recall"),
            chat_type=str(body.get("chat_type") or "group"),
            chat_id=str(body.get("chat_id") or body.get("group_id") or ""),
            user_id=str(body.get("user_id") or ""),
            operator_id=str(body.get("operator_id") or ""),
            target_id=str(body.get("target_id") or ""),
            message_id=str(body.get("message_id") or ""),
            extra=dict(body.get("extra") or {}),
        )
        await self.ctx.emit_notice(notice)

    async def _ingest_request(self, body: Dict[str, Any]) -> None:
        await self.ctx.emit_request(
            {
                "type": str(body.get("request_type") or "friend"),
                "sub_type": str(body.get("sub_type") or "add"),
                "user_id": str(body.get("user_id") or ""),
                "group_id": str(body.get("group_id") or ""),
                "comment": str(body.get("comment") or ""),
                "flag": str(body.get("flag") or ""),
            }
        )

    async def _ingest_sync(self, body: Dict[str, Any]) -> None:
        for item in body.get("friends") or []:
            user_id = str(item.get("user_id") or "")
            if user_id:
                self._friends[user_id] = Friend(user_id, str(item.get("nickname") or user_id), str(item.get("remark") or ""))
        for item in body.get("groups") or []:
            group_id = str(item.get("group_id") or "")
            if not group_id:
                continue
            members = [
                GroupMember(
                    str(m.get("user_id") or ""),
                    str(m.get("nickname") or m.get("user_id") or ""),
                    str(m.get("card") or ""),
                    str(m.get("role") or "member"),
                )
                for m in (item.get("members") or [])
                if m.get("user_id")
            ]
            self._groups[group_id] = Group(group_id, str(item.get("name") or group_id), str(item.get("owner") or ""), members)

    # -- 查询 --------------------------------------------------------------
    async def get_friend_list(self) -> List[Friend]:
        return list(self._friends.values())

    async def get_group_list(self) -> List[Group]:
        return list(self._groups.values())

    async def get_group(self, group_id: str) -> Group:
        group = self._groups.get(str(group_id))
        if group is None:
            raise DriverError(f"桥接端未同步群 {group_id}（先发 sync 事件）")
        return group

    async def get_group_members(self, group_id: str) -> List[GroupMember]:
        return (await self.get_group(group_id)).members

    # -- 出方向 ------------------------------------------------------------
    async def send_message(self, chat_type: str, chat_id: str, segments: List[Segment]) -> Tuple[Optional[str], int]:
        task = {
            "action": "send_message",
            "chat_type": chat_type,
            "chat_id": str(chat_id),
            "segments": [seg.to_dict() for seg in segments],
        }
        result = await self._dispatch(task)
        platform_id = str(result.get("platform_id") or result.get("message_id") or "") or None
        return platform_id, 0

    async def revoke(self, chat_type: str, chat_id: str, platform_message_id: str) -> None:
        await self._dispatch(
            {
                "action": "revoke",
                "chat_type": chat_type,
                "chat_id": str(chat_id),
                "platform_message_id": str(platform_message_id),
            }
        )

    async def _dispatch(self, task: Dict[str, Any]) -> Dict[str, Any]:
        task_id = next(self._counter)
        task["id"] = task_id
        task["time"] = int(time.time())
        if self.action_callback_url:
            result = await self._post_callback(task)
            if result is not None:
                return result
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[task_id] = future
        try:
            await asyncio.wait_for(self._queue.put(task), timeout=self.action_timeout)
        except asyncio.QueueFull as exc:
            self._pending.pop(task_id, None)
            raise DriverError("发送队列已满，hook 程序没有及时拉取任务") from exc
        try:
            return await asyncio.wait_for(future, timeout=self.action_timeout)
        except asyncio.TimeoutError as exc:
            raise DriverError(f"等待 hook 程序处理任务超时（{self.action_timeout}s）") from exc
        finally:
            self._pending.pop(task_id, None)

    async def _post_callback(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        import json
        import urllib.request

        data = json.dumps(task, ensure_ascii=False).encode("utf-8")

        def _do() -> bytes:
            request = urllib.request.Request(
                self.action_callback_url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=self.action_timeout) as response:
                return response.read()

        try:
            raw = await asyncio.to_thread(_do)
        except Exception as exc:
            self.log.warning("动作回调失败，退回队列模式：%s", exc)
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    # -- HTTP：任务队列 ----------------------------------------------------
    async def _on_actions(self, request: RawRequest):
        self._check_token(request)
        wait = min(30.0, max(0.0, float(request.query_one("wait") or 0)))
        try:
            task = await asyncio.wait_for(self._queue.get(), timeout=wait) if wait > 0 else self._queue.get_nowait()
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return json_response({"tasks": []})
        return json_response({"tasks": [task]})

    async def _on_action_result(self, request: RawRequest):
        self._check_token(request)
        body = request.json()
        task_id = int(body.get("id") or 0)
        future = self._pending.get(task_id)
        if future and not future.done():
            future.set_result(body)
        return json_response({"ok": True})

    async def _on_state(self, request: RawRequest):
        self._check_token(request)
        return json_response(
            {
                "account": {"user_id": self.self_id, "nickname": self.nickname},
                "friends": len(self._friends),
                "groups": len(self._groups),
                "queued": self._queue.qsize(),
                "pending": len(self._pending),
            }
        )

    async def handle_friend_request(self, flag: str, approve: bool, remark: str = "") -> None:
        await self._dispatch({"action": "friend_request", "flag": flag, "approve": approve, "remark": remark})

    async def handle_group_request(self, flag: str, sub_type: str, approve: bool, reason: str = "") -> None:
        await self._dispatch(
            {"action": "group_request", "flag": flag, "sub_type": sub_type, "approve": approve, "reason": reason}
        )

    async def set_group_kick(self, group_id: str, user_id: str, reject_add: bool = False) -> None:
        await self._dispatch({"action": "group_kick", "chat_id": group_id, "user_id": user_id, "reject_add": reject_add})

    async def set_group_ban(self, group_id: str, user_id: str, duration: int) -> None:
        await self._dispatch({"action": "group_ban", "chat_id": group_id, "user_id": user_id, "duration": duration})

    async def set_group_name(self, group_id: str, name: str) -> None:
        await self._dispatch({"action": "group_rename", "chat_id": group_id, "name": name})
