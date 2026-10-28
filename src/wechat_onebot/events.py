"""OneBot 11 事件构造。

字段对齐 NapCat / OneBot 11，保证 koishi-plugin-adapter-napcat 能直接消费。
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .model import ChatMessage, Group, GroupMember, LoginInfo, Notice
from .segments import to_cq


def _base(post_type: str, self_id: str) -> Dict[str, Any]:
    return {"time": int(time.time()), "self_id": str(self_id), "post_type": post_type}


def message_event(
    message: ChatMessage,
    message_id: int,
    self_id: str,
    post_format: str = "array",
    is_self_sent: bool = False,
    target_id: Optional[str] = None,
    quote_id: Optional[int] = None,
) -> Dict[str, Any]:
    event = _base("message_sent" if is_self_sent else "message", self_id)
    segments: List[Dict[str, Any]] = []
    if quote_id is not None:
        segments.append({"type": "reply", "data": {"id": str(quote_id)}})
    segments.extend(seg.to_dict() for seg in message.segments)
    event.update(
        {
            "message_type": message.chat_type,
            "sub_type": "normal" if message.chat_type == "group" else "friend",
            "message_id": message_id,
            "real_id": message_id,
            "message_seq": message_id,
            "user_id": str(message.sender.user_id),
            "sender": message.sender.to_dict(),
            "message": segments,
            "raw_message": _segments_to_cq(segments),
            "font": 0,
            "format": post_format,
            "time": int(message.time or time.time()),
        }
    )
    if message.chat_type == "group":
        event["group_id"] = str(message.chat_id)
    else:
        event["target_id"] = str(target_id or message.chat_id)
    return event


def lifecycle_event(self_id: str, sub_type: str = "connect") -> Dict[str, Any]:
    event = _base("meta_event", self_id)
    event.update({"meta_event_type": "lifecycle", "sub_type": sub_type})
    return event


def heartbeat_event(self_id: str, interval_ms: int = 30000) -> Dict[str, Any]:
    event = _base("meta_event", self_id)
    event.update(
        {
            "meta_event_type": "heartbeat",
            "status": {
                "online": True,
                "good": True,
                "app_name": "wechat-onebot",
                "app_version": __version__,
            },
            "interval": interval_ms,
        }
    )
    return event


def notice_event(notice: Notice, self_id: str, message_id: Optional[int] = None) -> Dict[str, Any]:
    event = _base("notice", self_id)
    kind = notice.kind
    event["notice_type"] = kind
    if notice.chat_type == "group" and notice.chat_id:
        event["group_id"] = str(notice.chat_id)
    if notice.user_id:
        event["user_id"] = str(notice.user_id)
    if notice.operator_id:
        event["operator_id"] = str(notice.operator_id)
    if notice.message_id:
        try:
            event["message_id"] = int(notice.message_id)
        except (TypeError, ValueError):
            event["message_id"] = notice.message_id
    elif message_id is not None:
        event["message_id"] = message_id

    if kind == "poke":
        event["notice_type"] = "notify"
        event["sub_type"] = "poke"
        event["target_id"] = str(notice.target_id or notice.user_id)
    elif kind == "group_card":
        event.setdefault("card", notice.extra.get("card", ""))
        event["card_new"] = notice.extra.get("card_new", event["card"])
    elif kind == "group_ban":
        event["duration"] = int(notice.extra.get("duration", 0))
    elif kind == "group_admin":
        event["sub_type"] = notice.extra.get("sub_type", "set")
    elif kind == "group_increase":
        event["sub_type"] = notice.extra.get("sub_type", "approve")
    elif kind == "group_decrease":
        event["sub_type"] = notice.extra.get("sub_type", "leave")
    for key, value in notice.extra.items():
        event.setdefault(key, value)
    return event


def friend_request_event(user_id: str, comment: str, flag: str, self_id: str) -> Dict[str, Any]:
    event = _base("request", self_id)
    event.update(
        {
            "request_type": "friend",
            "user_id": str(user_id),
            "comment": comment or "",
            "flag": flag,
        }
    )
    return event


def group_request_event(
    group_id: str, user_id: str, comment: str, flag: str, sub_type: str, self_id: str
) -> Dict[str, Any]:
    event = _base("request", self_id)
    event.update(
        {
            "request_type": "group",
            "sub_type": sub_type,  # add / invite
            "group_id": str(group_id),
            "user_id": str(user_id),
            "comment": comment or "",
            "flag": flag,
        }
    )
    return event


def group_info_dict(group: Group) -> Dict[str, Any]:
    return {
        "group_id": str(group.group_id),
        "group_name": group.name or group.group_id,
        "member_count": group.member_count,
        "max_member_count": max(group.member_count, 500),
        "owner_id": str(group.owner_id) if group.owner_id else "",
    }


def member_dict(group_id: str, member: GroupMember) -> Dict[str, Any]:
    return {
        "group_id": str(group_id),
        "user_id": str(member.user_id),
        "nickname": member.nickname or member.user_id,
        "card": member.card or member.nickname or member.user_id,
        "role": member.role,
        "join_time": int(member.join_time or 0),
        "last_sent_time": int(member.join_time or 0),
    }


def friend_dict(user_id: str, nickname: str, remark: str = "") -> Dict[str, Any]:
    return {
        "user_id": str(user_id),
        "nickname": nickname or user_id,
        "remark": remark or nickname or user_id,
    }


def login_dict(info: LoginInfo) -> Dict[str, Any]:
    return {"user_id": str(info.user_id), "nickname": info.nickname or str(info.user_id)}


def _segments_to_cq(segments: List[Dict[str, Any]]) -> str:
    from .model import Segment

    return to_cq([Segment.from_dict(item) for item in segments])


def render_for_format(event: Dict[str, Any], post_format: str) -> Dict[str, Any]:
    """按连接配置输出 array / string 两种 message 形式。"""
    if post_format != "string" or not isinstance(event.get("message"), list):
        return event
    rendered = copy.deepcopy(event)
    text = _segments_to_cq(event["message"])
    rendered["message"] = text
    rendered["raw_message"] = text
    rendered["format"] = "string"
    return rendered
