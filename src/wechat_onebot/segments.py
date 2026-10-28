"""OneBot 11 消息段 / CQ 码编解码。

入方向：Koishi 适配器发来的 `message` 可能是数组，也可能是 CQ 字符串；
出方向：本网关发给 Koishi 的消息默认数组（可在配置里改成 string）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from .model import Segment

CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[A-Za-z0-9_.-]+=[^,\]]*)*)\]")


def escape(value: Any, inline: bool = False) -> str:
    text = str(value)
    text = text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    if inline:
        text = text.replace(",", "&#44;")
    return text


def unescape(value: str) -> str:
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _attrs_to_str(data: Dict[str, Any]) -> str:
    parts = []
    for key, value in data.items():
        if value is None or value == "":
            continue
        parts.append(f",{key}={escape(value, inline=True)}")
    return "".join(parts)


def to_cq(segments: Iterable[Segment], auto_escape: bool = False) -> str:
    output = []
    for seg in segments:
        if seg.type == "text":
            text = str(seg.data.get("text", ""))
            output.append(escape(text) if auto_escape else text)
        else:
            output.append(f"[CQ:{seg.type}{_attrs_to_str(seg.data)}]")
    return "".join(output)


def from_cq(source: str, auto_escape: bool = False) -> List[Segment]:
    segments: List[Segment] = []
    rest = source
    while True:
        match = CQ_PATTERN.search(rest)
        if not match:
            break
        if match.start() > 0:
            prefix = rest[: match.start()]
            segments.append(Segment("text", {"text": prefix if auto_escape else unescape(prefix)}))
        seg_type = match.group(1)
        data: Dict[str, Any] = {}
        raw_attrs = match.group(2) or ""
        for chunk in raw_attrs.split(","):
            if not chunk or "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            data[key] = unescape(value)
        segments.append(Segment(seg_type, data))
        rest = rest[match.end() :]
    if rest:
        segments.append(Segment("text", {"text": rest if auto_escape else unescape(rest)}))
    return segments


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def normalize_message(message: Any, auto_escape: bool = False) -> List[Segment]:
    """把 OneBot 的 message 字段（数组 / CQ 串 / 纯文本）统一成消息段列表。"""
    if message is None:
        return []
    if isinstance(message, str):
        if auto_escape:
            return [Segment("text", {"text": message})] if message else []
        # 没有 CQ 码时直接当纯文本
        if "[CQ:" not in message:
            return [Segment("text", {"text": unescape(message)})] if message else []
        return from_cq(message)
    if isinstance(message, dict):
        # 兼容 {"type": "...", "data": {...}} 单段写法
        if "type" in message:
            return [_from_onebot_segment(message)]
        raise ValueError("message 字段格式不正确")
    if isinstance(message, (list, tuple)):
        segments: List[Segment] = []
        for item in message:
            if isinstance(item, str):
                segments.append(Segment("text", {"text": item}))
            elif isinstance(item, dict):
                segments.append(_from_onebot_segment(item))
            else:
                raise ValueError(f"无法识别的消息段: {item!r}")
        return _merge_text(segments)
    raise ValueError(f"无法识别的 message: {message!r}")


def _from_onebot_segment(raw: Dict[str, Any]) -> Segment:
    seg_type = str(raw.get("type") or "")
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {k: v for k, v in raw.items() if k != "type"}
    if seg_type == "text":
        text = data.get("text", data.get("content", ""))
        return Segment("text", {"text": _normalize_text(text)})
    if seg_type in ("img", "image"):
        return Segment("image", {k: v for k, v in data.items() if v is not None})
    if seg_type in ("audio", "record"):
        return Segment("record", {k: v for k, v in data.items() if v is not None})
    if seg_type in ("at", "mention"):
        qq = data.get("qq", data.get("user_id", ""))
        return Segment("at", {"qq": str(qq) if qq is not None else "", "name": data.get("name", "")})
    if seg_type == "quote":
        return Segment("reply", {"id": str(data.get("id", ""))})
    return Segment(seg_type, {k: v for k, v in data.items() if v is not None})


def _merge_text(segments: List[Segment]) -> List[Segment]:
    merged: List[Segment] = []
    for seg in segments:
        if seg.type == "text" and merged and merged[-1].type == "text":
            merged[-1].data["text"] += seg.data.get("text", "")
        else:
            merged.append(seg)
    return [seg for seg in merged if seg.type != "text" or seg.data.get("text")]


def to_plain_text(segments: Iterable[Segment], include_media: bool = False) -> str:
    """把消息段拍平成便于日志 / 微信文本发送的纯文本。"""
    pieces: List[str] = []
    for seg in segments:
        if seg.type == "text":
            pieces.append(seg.data.get("text", ""))
        elif seg.type == "at":
            qq = seg.data.get("qq", "")
            name = seg.data.get("name") or qq
            pieces.append("@全体成员" if qq == "all" else f"@{name}")
        elif seg.type == "image":
            pieces.append("[图片]")
        elif seg.type == "record":
            pieces.append("[语音]")
        elif seg.type == "video":
            pieces.append("[视频]")
        elif seg.type == "file":
            pieces.append(f"[文件] {seg.data.get('name') or seg.data.get('file', '')}")
        elif seg.type == "face":
            pieces.append("[表情]")
        elif seg.type == "reply" and include_media:
            pieces.append("[引用]")
    return "".join(pieces).strip()


def split_at(segments: Iterable[Segment]) -> (List[str], List[Segment]):
    """拆出 @ 目标（微信发文本时用 At 参数），其余段保持顺序。"""
    at_ids: List[str] = []
    rest: List[Segment] = []
    for seg in segments:
        if seg.type == "at":
            qq = str(seg.data.get("qq", "")).strip()
            if qq:
                at_ids.append(qq)
            continue
        rest.append(seg)
    return at_ids, rest


def first_mention(segments: Iterable[Segment]) -> Optional[str]:
    for seg in segments:
        if seg.type == "at":
            return str(seg.data.get("qq", ""))
    return None


def media_segments(segments: Iterable[Segment]) -> List[Segment]:
    return [seg for seg in segments if seg.type in ("image", "record", "video", "file")]


def build_text(text: str) -> List[Segment]:
    return [Segment("text", {"text": text})] if text else []
