"""OneBot 11 动作分发（API）。"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .logger import get_logger
from .model import ChatMessage, DriverError, Segment, Sender, UnsupportedAction
from .segments import normalize_message, to_plain_text

log = get_logger("action")

OK = 0
RETCODE_FAILED = 100
RETCODE_UNSUPPORTED = 1404
RETCODE_BAD_REQUEST = 400


class ActionHandler:
    def __init__(self, gateway) -> None:
        self.gateway = gateway

    # -- 入口 --------------------------------------------------------------
    async def dispatch(self, action: str, params: Optional[Dict[str, Any]], echo: Any = None) -> Dict[str, Any]:
        params = params or {}
        handler = self._resolve(action)
        if handler is None:
            return self._fail(RETCODE_UNSUPPORTED, f"不支持的 API: {action}", echo)
        try:
            data = await handler(params)
            return {"status": "ok", "retcode": OK, "data": data, "echo": echo, "wording": ""}
        except UnsupportedAction as exc:
            return self._fail(RETCODE_UNSUPPORTED, str(exc), echo)
        except DriverError as exc:
            return self._fail(RETCODE_FAILED, str(exc), echo)
        except (ValueError, TypeError, KeyError) as exc:
            return self._fail(RETCODE_BAD_REQUEST, f"参数错误: {exc}", echo)
        except Exception as exc:  # pragma: no cover - 防御性
            log.exception("API %s 执行失败", action)
            return self._fail(RETCODE_FAILED, f"内部错误: {exc}", echo)

    def _fail(self, retcode: int, message: str, echo: Any) -> Dict[str, Any]:
        return {"status": "failed", "retcode": retcode, "data": None, "echo": echo, "wording": message, "message": message}

    def _resolve(self, action: str):
        mapping = {
            "send_msg": self._send_msg,
            "send_private_msg": self._send_private_msg,
            "send_group_msg": self._send_group_msg,
            "delete_msg": self._delete_msg,
            "get_msg": self._get_msg,
            "get_login_info": self._get_login_info,
            "get_status": self._get_status,
            "get_version_info": self._get_version_info,
            "get_friend_list": self._get_friend_list,
            "get_group_list": self._get_group_list,
            "get_group_info": self._get_group_info,
            "get_group_member_info": self._get_group_member_info,
            "get_group_member_list": self._get_group_member_list,
            "get_stranger_info": self._get_stranger_info,
            "can_send_image": lambda _: self._yes(),
            "can_send_record": lambda _: self._yes(),
            "get_image": self._get_image,
            "get_record": self._get_record,
            "mark_msg_as_read": lambda _: None,
            "clean_cache": self._clean_cache,
            "reload_event_filter": lambda _: None,
            "set_restart": lambda _: None,
            "get_group_msg_history": self._get_group_msg_history,
            "get_friend_msg_history": self._get_friend_msg_history,
            "set_group_card": self._set_group_card,
            "set_group_ban": self._set_group_ban,
            "set_group_whole_ban": self._set_group_whole_ban,
            "set_group_kick": self._set_group_kick,
            "set_group_leave": self._set_group_leave,
            "set_group_name": self._set_group_name,
            "set_group_admin": self._set_group_admin,
            "set_friend_add_request": self._set_friend_add_request,
            "set_group_add_request": self._set_group_add_request,
            "upload_group_file": self._upload_file,
            "upload_private_file": self._upload_file,
            "get_online_clients": self._get_online_clients,
            "get_group_at_all_remain": self._get_at_all_remain,
            "get_cookies": lambda _: self._unsupported("微信没有 cookies 概念"),
            "get_csrf_token": lambda _: self._unsupported("微信没有 csrf token 概念"),
            "get_credentials": lambda _: self._unsupported("微信没有 credentials 概念"),
            "send_group_forward_msg": lambda _: self._unsupported("微信不支持合并转发"),
            "send_private_forward_msg": lambda _: self._unsupported("微信不支持合并转发"),
            "get_forward_msg": lambda _: self._unsupported("微信不支持合并转发"),
        }
        return mapping.get(action)

    # -- 发送 --------------------------------------------------------------
    async def _send(self, chat_type: str, chat_id: str, message: Any, auto_escape: bool = False) -> Dict[str, Any]:
        if not chat_id:
            raise ValueError("缺少会话 ID")
        segments: List[Segment] = normalize_message(message, auto_escape=auto_escape)
        if not segments:
            raise ValueError("消息为空")
        platform_id, _ = await self.gateway.driver.send_message(chat_type, str(chat_id), segments)
        stored = await self.gateway.record_self_message(chat_type, str(chat_id), segments, platform_id)
        preview = to_plain_text(segments)
        log.info("发送%s消息 -> %s: %s", "群" if chat_type == "group" else "私聊", chat_id, preview or "[富媒体]")
        return {"message_id": stored, "platform_message_id": platform_id}

    async def _send_msg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if params.get("group_id"):
            return await self._send("group", str(params["group_id"]), params.get("message"), bool(params.get("auto_escape")))
        if params.get("user_id"):
            return await self._send("private", str(params["user_id"]), params.get("message"), bool(params.get("auto_escape")))
        raise ValueError("需要 group_id 或 user_id")

    async def _send_private_msg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self._send("private", str(params.get("user_id", "")), params.get("message"), bool(params.get("auto_escape")))

    async def _send_group_msg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self._send("group", str(params.get("group_id", "")), params.get("message"), bool(params.get("auto_escape")))

    async def _delete_msg(self, params: Dict[str, Any]) -> None:
        message_id = int(params.get("message_id", 0))
        stored = self.gateway.store.get(message_id)
        if stored is None:
            raise ValueError(f"找不到消息 {message_id}")
        message = stored.message
        if message.platform_id:
            await self.gateway.driver.revoke(message.chat_type, message.chat_id, str(message.platform_id))
        self.gateway.store.drop(message_id)

    async def _get_msg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        message_id = int(params.get("message_id", 0))
        stored = self.gateway.store.get(message_id)
        if stored is None:
            raise ValueError(f"找不到消息 {message_id}")
        return self.gateway.event_for_message(stored)

    # -- 查询 --------------------------------------------------------------
    async def _get_login_info(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {"user_id": str(self.gateway.self_id), "nickname": self.gateway.nickname}

    async def _get_status(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        online = self.gateway.online and self.gateway.logged_in
        return {
            "online": online,
            "good": online,
            "app_initialized": online,
            "app_enabled": online,
            "plugins_good": True,
            "stat": {
                "packet_received": self.gateway.stats["received"],
                "packet_sent": self.gateway.stats["sent"],
                "message_received": self.gateway.stats["messages"],
                "message_sent": self.gateway.stats["sent_messages"],
            },
        }

    async def _get_version_info(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "app_name": "wechat-onebot",
            "app_version": __version__,
            "app_full_name": "wechat-onebot (WeChat OneBot 11 adapter)",
            "protocol_version": "v11",
            "version": __version__,
            "driver": self.gateway.driver.name,
        }

    async def _get_friend_list(self, _params: Dict[str, Any]) -> List[Dict[str, Any]]:
        friends = await self.gateway.driver.get_friend_list()
        return [
            {"user_id": f.user_id, "nickname": f.nickname or f.user_id, "remark": f.remark or f.nickname or f.user_id}
            for f in friends
        ]

    async def _get_group_list(self, _params: Dict[str, Any]) -> List[Dict[str, Any]]:
        from .events import group_info_dict

        groups = await self.gateway.driver.get_group_list()
        return [group_info_dict(group) for group in groups]

    async def _get_group_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from .events import group_info_dict

        group = await self.gateway.driver.get_group(str(params.get("group_id", "")))
        return group_info_dict(group)

    async def _get_group_member_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from .events import member_dict

        group_id = str(params.get("group_id", ""))
        member = await self.gateway.driver.get_group_member(group_id, str(params.get("user_id", "")))
        return member_dict(group_id, member)

    async def _get_group_member_list(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        from .events import member_dict

        group_id = str(params.get("group_id", ""))
        members = await self.gateway.driver.get_group_members(group_id)
        return [member_dict(group_id, member) for member in members]

    async def _get_stranger_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        user_id = str(params.get("user_id", ""))
        try:
            friend = await self.gateway.driver.get_friend(user_id)
            return {"user_id": user_id, "nickname": friend.nickname or user_id, "remark": friend.remark or ""}
        except (DriverError, UnsupportedAction):
            return {"user_id": user_id, "nickname": user_id, "remark": ""}

    def _yes(self) -> Dict[str, Any]:
        return {"yes": True}

    async def _get_image(self, params: Dict[str, Any]) -> Dict[str, Any]:
        file = str(params.get("file", ""))
        return {"file": file, "url": file}

    async def _get_record(self, params: Dict[str, Any]) -> Dict[str, Any]:
        file = str(params.get("file", ""))
        return {"file": file, "url": file}

    def _clean_cache(self, _params: Dict[str, Any]) -> None:
        self.gateway.media.clear()

    def _unsupported(self, message: str):
        raise UnsupportedAction(message)

    # -- 历史 --------------------------------------------------------------
    async def _get_group_msg_history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._history("group", str(params.get("group_id", "")), params)

    async def _get_friend_msg_history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._history("private", str(params.get("user_id", "")), params)

    def _history(self, chat_type: str, chat_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        count = int(params.get("count") or 20)
        stored = self.gateway.store.history(chat_type, chat_id, count)
        return {"messages": [self.gateway.event_for_message(item) for item in stored]}

    # -- 群管理 ------------------------------------------------------------
    async def _set_group_card(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.set_group_card(
            str(params.get("group_id", "")), str(params.get("user_id", "")), str(params.get("card", ""))
        )

    async def _set_group_ban(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.set_group_ban(
            str(params.get("group_id", "")), str(params.get("user_id", "")), int(params.get("duration") or 0)
        )

    async def _set_group_whole_ban(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.set_group_whole_ban(str(params.get("group_id", "")), bool(params.get("enable")))

    async def _set_group_kick(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.set_group_kick(
            str(params.get("group_id", "")), str(params.get("user_id", "")), bool(params.get("reject_add_request"))
        )

    async def _set_group_leave(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.set_group_leave(str(params.get("group_id", "")))

    async def _set_group_name(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.set_group_name(str(params.get("group_id", "")), str(params.get("group_name", "")))

    async def _set_group_admin(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.set_group_admin(
            str(params.get("group_id", "")), str(params.get("user_id", "")), bool(params.get("enable"))
        )

    async def _set_friend_add_request(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.handle_friend_request(
            str(params.get("flag", "")), bool(params.get("approve")), str(params.get("remark", ""))
        )

    async def _set_group_add_request(self, params: Dict[str, Any]) -> None:
        await self.gateway.driver.handle_group_request(
            str(params.get("flag", "")),
            str(params.get("sub_type") or "add"),
            bool(params.get("approve")),
            str(params.get("reason", "")),
        )

    # -- 文件 --------------------------------------------------------------
    async def _upload_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        file = str(params.get("file", ""))
        name = str(params.get("name") or file.rsplit("/", 1)[-1] or "file")
        segment = Segment("file", {"file": file, "name": name})
        if params.get("group_id"):
            chat_type, chat_id = "group", str(params["group_id"])
        else:
            chat_type, chat_id = "private", str(params.get("user_id", ""))
        platform_id, _ = await self.gateway.driver.send_message(chat_type, chat_id, [segment])
        stored = await self.gateway.record_self_message(chat_type, chat_id, [segment], platform_id)
        return {"message_id": stored, "file_id": platform_id or ""}

    # -- 其他 --------------------------------------------------------------
    async def _get_online_clients(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {"clients": self.gateway.client_summary()}

    async def _get_at_all_remain(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {"can_at_all": True, "remain_at_all_count_for_group": 10, "remain_at_all_count_for_uin": 10}
