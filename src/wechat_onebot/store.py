"""消息存档：OneBot 的 message_id <-> 微信侧消息 ID 映射，供 get_msg / 撤回 / 引用使用。"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .model import ChatMessage, Segment

MAX_MESSAGES = 5000


@dataclass
class StoredMessage:
    onebot_id: int
    message: ChatMessage

    def to_dict(self) -> dict:
        return {
            "message_id": self.onebot_id,
            "platform_id": self.message.platform_id,
            "chat_type": self.message.chat_type,
            "chat_id": self.message.chat_id,
            "sender_id": self.message.sender.user_id,
            "sender_name": self.message.sender.card or self.message.sender.nickname,
            "time": self.message.time,
            "segments": [seg.to_dict() for seg in self.message.segments],
            "is_self": self.message.is_self,
        }


class MessageStore:
    def __init__(self, capacity: int = MAX_MESSAGES) -> None:
        self.capacity = capacity
        self._by_onebot: Dict[int, StoredMessage] = {}
        self._by_platform: Dict[str, int] = {}
        self._history: Dict[str, List[int]] = {}
        self._counter = itertools.count(1)

    def _key(self, chat_type: str, chat_id: str) -> str:
        return f"{chat_type}:{chat_id}"

    def remember(self, message: ChatMessage) -> int:
        platform_id = str(message.platform_id or "")
        if platform_id and platform_id in self._by_platform:
            existing = self._by_platform[platform_id]
            if existing in self._by_onebot:
                return existing
        onebot_id = next(self._counter)
        stored = StoredMessage(onebot_id, message)
        self._by_onebot[onebot_id] = stored
        if platform_id:
            self._by_platform[platform_id] = onebot_id
        key = self._key(message.chat_type, message.chat_id)
        self._history.setdefault(key, []).append(onebot_id)
        self._trim(key)
        if len(self._by_onebot) > self.capacity:
            self._evict_oldest()
        return onebot_id

    def get(self, onebot_id: int) -> Optional[StoredMessage]:
        return self._by_onebot.get(int(onebot_id))

    def by_platform(self, platform_id: str) -> Optional[StoredMessage]:
        onebot_id = self._by_platform.get(str(platform_id))
        return self._by_onebot.get(onebot_id) if onebot_id else None

    def history(self, chat_type: str, chat_id: str, count: int = 20) -> List[StoredMessage]:
        ids = self._history.get(self._key(chat_type, chat_id), [])
        if count <= 0:
            return []
        selected = ids[-count:]
        return [self._by_onebot[i] for i in selected if i in self._by_onebot]

    def latest(self, chat_type: str, chat_id: str, sender_id: Optional[str] = None) -> Optional[StoredMessage]:
        ids = self._history.get(self._key(chat_type, chat_id), [])
        for onebot_id in reversed(ids):
            stored = self._by_onebot.get(onebot_id)
            if stored is None:
                continue
            if sender_id is None or stored.message.sender.user_id == sender_id:
                return stored
        return None

    def drop(self, onebot_id: int) -> None:
        stored = self._by_onebot.pop(int(onebot_id), None)
        if stored is None:
            return
        platform_id = str(stored.message.platform_id or "")
        if platform_id and self._by_platform.get(platform_id) == int(onebot_id):
            self._by_platform.pop(platform_id, None)
        key = self._key(stored.message.chat_type, stored.message.chat_id)
        ids = self._history.get(key)
        if ids and int(onebot_id) in ids:
            ids.remove(int(onebot_id))

    def _trim(self, key: str) -> None:
        ids = self._history.get(key) or []
        if len(ids) > 200:
            self._history[key] = ids[-200:]

    def _evict_oldest(self) -> None:
        oldest = min(self._by_onebot) if self._by_onebot else None
        if oldest is not None:
            self.drop(oldest)

    def stats(self) -> dict:
        return {
            "cached": len(self._by_onebot),
            "chats": len(self._history),
            "capacity": self.capacity,
        }


def render_message(segments: List[Segment], chat_type: str, chat_id: str) -> dict:
    """供 WebUI 展示用的简化结构。"""
    return {
        "chat_type": chat_type,
        "chat_id": chat_id,
        "segments": [seg.to_dict() for seg in segments],
        "text": "".join(seg.data.get("text", "") for seg in segments if seg.type == "text"),
        "time": int(time.time()),
    }
