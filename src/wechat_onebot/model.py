"""内部数据模型（与 OneBot 解耦，驱动层只依赖这些结构）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class DriverError(Exception):
    """驱动调用失败（网络、协议、账号未登录等）。"""


class UnsupportedAction(Exception):
    """当前驱动不支持该 OneBot 动作。"""


@dataclass
class Segment:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "data": dict(self.data)}

    @classmethod
    def from_dict(cls, raw: Any) -> "Segment":
        if not isinstance(raw, dict):
            raise ValueError("消息段必须是对象")
        seg_type = str(raw.get("type") or "")
        if not seg_type:
            raise ValueError("消息段缺少 type")
        data = raw.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("消息段 data 必须是对象")
        return cls(seg_type, dict(data))


@dataclass
class Sender:
    user_id: str
    nickname: str = ""
    card: str = ""
    role: str = "member"  # owner / admin / member

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "card": self.card or self.nickname,
            "role": self.role,
        }


@dataclass
class ChatMessage:
    """驱动层收到的（或自己发出的）一条微信消息。"""

    platform_id: str
    chat_type: str  # group / private
    chat_id: str
    sender: Sender
    segments: List[Segment]
    time: int = field(default_factory=lambda: int(time.time()))
    is_self: bool = False
    quote_platform_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(seg.data.get("text", "") for seg in self.segments if seg.type == "text")


@dataclass
class Friend:
    user_id: str
    nickname: str = ""
    remark: str = ""
    avatar: str = ""

    def display_name(self) -> str:
        return self.remark or self.nickname or self.user_id


@dataclass
class GroupMember:
    user_id: str
    nickname: str = ""
    card: str = ""
    role: str = "member"
    join_time: int = 0

    def display_name(self) -> str:
        return self.card or self.nickname or self.user_id


@dataclass
class Group:
    group_id: str
    name: str = ""
    owner_id: str = ""
    members: List[GroupMember] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.members)

    def member(self, user_id: str) -> Optional[GroupMember]:
        for item in self.members:
            if item.user_id == user_id:
                return item
        return None


@dataclass
class Notice:
    """微信侧的通知类事件。"""

    kind: str  # group_recall / friend_recall / group_increase / group_decrease /
    #            group_card / group_ban / friend_add / poke / group_admin
    chat_type: str = "group"
    chat_id: str = ""
    user_id: str = ""
    operator_id: str = ""
    target_id: str = ""
    message_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoginInfo:
    user_id: str
    nickname: str = ""
