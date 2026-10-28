"""mock 驱动：本地模拟一个微信账号，用来在没有任何真实微信后端时跑通 OneBot / Koishi。

特点：
  * 自带好友、群、群成员，支持收发文字 / 图片 / 语音 / 视频标记
  * 支持群管理动作（禁言、踢人、改名片、改群名…）
  * 支持交互式控制台（配置 mock.interactive），方便直接伪造“别人发来的消息”
  * WebUI 的“模拟消息”面板也走这里
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import time
from typing import Any, Dict, List, Optional

from ..model import (
    ChatMessage,
    Friend,
    Group,
    GroupMember,
    LoginInfo,
    Notice,
    Segment,
    Sender,
    UnsupportedAction,
)
from ..segments import to_plain_text
from .base import BaseDriver


class MockDriver(BaseDriver):
    name = "mock"

    def __init__(self, config: Dict[str, Any], ctx: Any) -> None:
        super().__init__(config, ctx)
        self.capabilities = {
            "text",
            "image",
            "record",
            "video",
            "file",
            "at",
            "reply",
            "revoke",
            "group_manage",
            "friend_request",
            "group_request",
        }
        account = (ctx.config.account() if ctx is not None else {}) or {}
        self.self_id = account.get("wxid") or "wxid_mock_bot"
        self.nickname = account.get("nickname") or "微信小助手"
        self._friends: Dict[str, Friend] = {}
        self._groups: Dict[str, Group] = {}
        self._counter = itertools.count(1)
        self._console_task: Optional[asyncio.Task] = None
        self._whole_ban: set = set()
        self._load_seed()

    # -- 初始化 ------------------------------------------------------------
    def _load_seed(self) -> None:
        friends = self.config.get("friends") or [
            {"user_id": "wxid_xiaoai", "nickname": "小爱"},
            {"user_id": "wxid_abobo", "nickname": "阿波"},
        ]
        for item in friends:
            user_id = str(item.get("user_id") or "").strip()
            if not user_id:
                continue
            self._friends[user_id] = Friend(
                user_id=user_id,
                nickname=str(item.get("nickname") or user_id),
                remark=str(item.get("remark") or ""),
                avatar=str(item.get("avatar") or ""),
            )
        groups = self.config.get("groups") or [
            {
                "group_id": "12345678@chatroom",
                "name": "微信测试群",
                "owner": "wxid_xiaoai",
                "admins": [self.self_id],
                "members": [
                    {"user_id": "wxid_xiaoai", "nickname": "小爱"},
                    {"user_id": "wxid_abobo", "nickname": "阿波"},
                ],
            }
        ]
        for item in groups:
            group_id = str(item.get("group_id") or "").strip()
            if not group_id:
                continue
            members: List[GroupMember] = []
            for raw in item.get("members") or []:
                user_id = str(raw.get("user_id") or "").strip()
                if not user_id:
                    continue
                members.append(
                    GroupMember(
                        user_id=user_id,
                        nickname=str(raw.get("nickname") or user_id),
                        card=str(raw.get("card") or ""),
                    )
                )
            if not any(m.user_id == self.self_id for m in members):
                members.append(
                    GroupMember(self.self_id, self.nickname, "", role="admin", join_time=int(time.time()) - 86400)
                )
            owner = str(item.get("owner") or "")
            admins = {str(x) for x in (item.get("admins") or [])}
            admins.add(self.self_id)
            for member in members:
                if owner and member.user_id == owner:
                    member.role = "owner"
                elif member.user_id in admins:
                    member.role = "admin"
                else:
                    member.role = "member"
            self._groups[group_id] = Group(
                group_id=group_id,
                name=str(item.get("name") or group_id),
                owner_id=owner,
                members=members,
            )

    async def start(self) -> None:
        await super().start()
        self.log.info("mock 微信已上线：%s (%s)", self.nickname, self.self_id)
        if bool(self.config.get("interactive", True)):
            self._console_task = asyncio.create_task(self._console_loop())

    async def stop(self) -> None:
        await super().stop()
        if self._console_task:
            self._console_task.cancel()
            self._console_task = None

    # -- 登录 --------------------------------------------------------------
    async def login(self) -> LoginInfo:
        return LoginInfo(self.self_id, self.nickname)

    # -- 查询 --------------------------------------------------------------
    async def get_friend_list(self) -> List[Friend]:
        return list(self._friends.values())

    async def get_group_list(self) -> List[Group]:
        return [group for group in self._groups.values() if group.member(self.self_id)]

    def group(self, group_id: str) -> Group:
        group = self._groups.get(str(group_id))
        if group is None:
            raise UnsupportedAction(f"mock 中不存在群 {group_id}")
        return group

    def resolve_group(self, key: str) -> Optional[Group]:
        key = str(key)
        if key in self._groups:
            return self._groups[key]
        for group in self._groups.values():
            if group.name == key:
                return group
        return None

    def resolve_friend(self, key: str) -> Optional[Friend]:
        key = str(key)
        if key in self._friends:
            return self._friends[key]
        for friend in self._friends.values():
            if friend.nickname == key or friend.remark == key:
                return friend
        return None

    # -- 发送 --------------------------------------------------------------
    async def send_message(self, chat_type: str, chat_id: str, segments: List[Segment]) -> tuple:
        chat_id = str(chat_id)
        role = "member"
        if chat_type == "group":
            group = self.group(chat_id)
            member = group.member(self.self_id)
            role = member.role if member else "member"
        elif chat_type == "private":
            if chat_id not in self._friends:
                self._friends[chat_id] = Friend(chat_id, chat_id)
        else:
            raise UnsupportedAction(f"未知会话类型 {chat_type}")
        platform_id = f"mock_{next(self._counter)}_{int(time.time() * 1000)}"
        message = ChatMessage(
            platform_id=platform_id,
            chat_type=chat_type,
            chat_id=chat_id,
            sender=self._sender(role),
            segments=segments,
            time=int(time.time()),
            is_self=True,
        )
        self.log.info("mock 发送 -> %s(%s): %s", chat_type, chat_id, to_plain_text(segments) or "[富媒体]")
        # 自身消息由网关的动作层统一入库并广播（避免同一条消息重复上报）
        return platform_id, 0

    def _sender(self, role: str = "admin") -> Sender:
        return Sender(self.self_id, self.nickname, "", role=role)

    async def revoke(self, chat_type: str, chat_id: str, platform_message_id: str) -> None:
        await self.ctx.emit_notice(
            Notice(
                kind="group_recall" if chat_type == "group" else "friend_recall",
                chat_type=chat_type,
                chat_id=str(chat_id),
                user_id=self.self_id,
                operator_id=self.self_id,
                message_id=str(platform_message_id),
            )
        )

    # -- 群管理 ------------------------------------------------------------
    async def set_group_card(self, group_id: str, user_id: str, card: str) -> None:
        group = self.group(group_id)
        member = group.member(str(user_id))
        if member is None:
            raise UnsupportedAction("群成员不存在")
        old = member.card
        member.card = card
        await self.ctx.emit_notice(
            Notice("group_card", "group", group.group_id, user_id, self.self_id, extra={"card": old, "card_new": card})
        )

    async def set_group_ban(self, group_id: str, user_id: str, duration: int) -> None:
        group = self.group(group_id)
        member = group.member(str(user_id)) if user_id else None
        if user_id and member is None:
            raise UnsupportedAction("群成员不存在")
        target = str(user_id) if user_id else "0"
        await self.ctx.emit_notice(
            Notice("group_ban", "group", group.group_id, target, self.self_id, extra={"duration": duration})
        )

    async def set_group_whole_ban(self, group_id: str, enable: bool) -> None:
        group = self.group(group_id)
        if enable:
            self._whole_ban.add(group.group_id)
        else:
            self._whole_ban.discard(group.group_id)
        await self.ctx.emit_notice(
            Notice("group_ban", "group", group.group_id, "0", self.self_id, extra={"duration": 0 if not enable else -1})
        )

    async def set_group_kick(self, group_id: str, user_id: str, reject_add: bool = False) -> None:
        group = self.group(group_id)
        member = group.member(str(user_id))
        if member is None:
            raise UnsupportedAction("群成员不存在")
        group.members.remove(member)
        await self.ctx.emit_notice(
            Notice("group_decrease", "group", group.group_id, user_id, self.self_id, extra={"sub_type": "kick"})
        )

    async def set_group_leave(self, group_id: str) -> None:
        group = self.group(group_id)
        member = group.member(self.self_id)
        if member:
            group.members.remove(member)
        await self.ctx.emit_notice(
            Notice("group_decrease", "group", group.group_id, self.self_id, self.self_id, extra={"sub_type": "leave"})
        )

    async def set_group_name(self, group_id: str, name: str) -> None:
        group = self.group(group_id)
        group.name = name

    async def set_group_admin(self, group_id: str, user_id: str, enable: bool) -> None:
        group = self.group(group_id)
        member = group.member(str(user_id))
        if member is None:
            raise UnsupportedAction("群成员不存在")
        member.role = "admin" if enable else "member"
        await self.ctx.emit_notice(
            Notice(
                "group_admin",
                "group",
                group.group_id,
                user_id,
                self.self_id,
                extra={"sub_type": "set" if enable else "unset"},
            )
        )

    # -- 请求 --------------------------------------------------------------
    async def handle_friend_request(self, flag: str, approve: bool, remark: str = "") -> None:
        user_id = flag.split(":")[-1] or "wxid_unknown"
        if approve:
            self._friends[user_id] = Friend(user_id, user_id, remark)
            await self.ctx.emit_notice(Notice("friend_add", "private", "", user_id, self.self_id))
        self.log.info("mock 好友请求 %s -> %s", user_id, "同意" if approve else "拒绝")

    async def handle_group_request(self, flag: str, sub_type: str, approve: bool, reason: str = "") -> None:
        self.log.info("mock 群请求 %s (%s) -> %s", flag, sub_type, "同意" if approve else "拒绝")

    # -- 注入（WebUI / 控制台 / 测试） -------------------------------------
    async def inject_message(
        self,
        chat_type: str,
        chat_id: str,
        text: str = "",
        segments: Optional[List[Segment]] = None,
        sender_id: Optional[str] = None,
        sender_name: Optional[str] = None,
    ) -> ChatMessage:
        chat_id = str(chat_id)
        if chat_type == "group":
            group = self.group(chat_id)
            if sender_id:
                member = group.member(sender_id)
                if member is None:
                    member = GroupMember(sender_id, sender_name or sender_id)
                    group.members.append(member)
                role = member.role
                name = member.card or member.nickname
            else:
                candidates = [m for m in group.members if m.user_id != self.self_id]
                member = candidates[0] if candidates else group.members[0]
                sender_id = member.user_id
                name = member.card or member.nickname
                role = member.role
            sender = Sender(sender_id, name, "", role=role)
        else:
            friend = self._friends.get(chat_id)
            sender_id = sender_id or chat_id
            name = sender_name or (friend.nickname if friend else chat_id)
            sender = Sender(sender_id, name, "", role="member")
        if segments is None:
            segments = [Segment("text", {"text": text})]
        message = ChatMessage(
            platform_id=f"mock_{next(self._counter)}_{int(time.time() * 1000)}",
            chat_type=chat_type,
            chat_id=chat_id,
            sender=sender,
            segments=segments,
            time=int(time.time()),
            is_self=False,
        )
        self.log.info("mock 收到 <- %s(%s) %s: %s", chat_type, chat_id, sender.nickname, to_plain_text(segments) or "[富媒体]")
        await self.ctx.emit_message(message)
        return message

    async def inject_friend_request(self, user_id: str, comment: str = "请求添加你为好友") -> None:
        await self.ctx.emit_request({"type": "friend", "user_id": user_id, "comment": comment})

    # -- 控制台 ------------------------------------------------------------
    async def _console_loop(self) -> None:
        loop = asyncio.get_running_loop()
        self._print_help()
        while self._started:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except (RuntimeError, ValueError):
                return
            if not line:
                # stdin 已关闭（例如以守护进程方式运行），关掉控制台避免空转
                self.log.debug("控制台输入已关闭，停止 mock 控制台")
                return
            line = line.strip()
            if not line:
                continue
            try:
                await self._handle_console(line)
            except Exception as exc:
                print(f"  ! {exc}")

    def _print_help(self) -> None:
        print()
        print("  ┌─ mock 微信控制台 ────────────────────────────────")
        print("  │  g <群名/群ID> <内容>     模拟群里有人说话")
        print("  │  f <好友名/wxid> <内容>   模拟好友发来消息")
        print("  │  ls                       查看好友与群")
        print("  │  help                     显示帮助")
        print("  └──────────────────────────────────────────────")
        print()

    async def _handle_console(self, line: str) -> None:
        if line in ("help", "?", "h"):
            self._print_help()
            return
        if line in ("ls", "list"):
            print("  好友：")
            for friend in self._friends.values():
                print(f"    - {friend.user_id}  {friend.nickname} {('(' + friend.remark + ')') if friend.remark else ''}")
            print("  群：")
            for group in self._groups.values():
                names = ", ".join(f"{m.user_id}:{m.nickname}" for m in group.members)
                print(f"    - {group.group_id}  {group.name}  [{names}]")
            return
        parts = line.split(" ", 2)
        if len(parts) < 3 or parts[0] not in ("g", "f"):
            raise ValueError("格式：g <群> <内容> / f <好友> <内容>（help 查看帮助）")
        target, content = parts[1], parts[2]
        if parts[0] == "g":
            group = self.resolve_group(target)
            if group is None:
                raise ValueError(f"找不到群 {target}")
            await self.inject_message("group", group.group_id, content)
        else:
            friend = self.resolve_friend(target)
            if friend is None:
                raise ValueError(f"找不到好友 {target}")
            await self.inject_message("private", friend.user_id, content)
