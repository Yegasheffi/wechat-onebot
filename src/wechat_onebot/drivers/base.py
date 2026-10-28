"""微信驱动基类。

驱动负责与真正的微信侧打交道（模拟器 / iPad 协议 HTTP 服务 / 任意 hook 程序），
把收到的消息、通知、请求推给网关，并实现网关下发的发送 / 群管理动作。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..logger import get_logger
from ..model import (
    ChatMessage,
    DriverError,
    Friend,
    Group,
    GroupMember,
    LoginInfo,
    Notice,
    Segment,
    UnsupportedAction,
)

CAPABILITIES = [
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
]


class BaseDriver:
    name = "base"
    # 是否需要扫码登录（mock / bridge 视为已登录）
    supports_login = False

    def __init__(self, config: Dict[str, Any], ctx: Any) -> None:
        self.config = config or {}
        self.ctx = ctx  # 网关上下文
        self.log = get_logger(f"driver.{self.name}")
        self.capabilities: set = set()
        self._started = False

    # -- 生命周期 ----------------------------------------------------------
    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def login(self) -> LoginInfo:
        raise UnsupportedAction("驱动未实现 login()")

    # -- 发消息 ------------------------------------------------------------
    async def send_message(self, chat_type: str, chat_id: str, segments: List[Segment]) -> Tuple[Optional[str], int]:
        """发送消息，返回 (平台消息 ID, 0)。失败抛 DriverError。"""
        raise UnsupportedAction("该驱动暂不支持发送消息")

    async def revoke(self, chat_type: str, chat_id: str, platform_message_id: str) -> None:
        raise UnsupportedAction("该驱动暂不支持撤回消息")

    # -- 联系人 / 群 -------------------------------------------------------
    async def get_friend_list(self) -> List[Friend]:
        raise UnsupportedAction("该驱动暂不支持获取好友列表")

    async def get_friend(self, user_id: str) -> Friend:
        for friend in await self.get_friend_list():
            if friend.user_id == user_id:
                return friend
        raise DriverError(f"找不到好友 {user_id}")

    async def get_group_list(self) -> List[Group]:
        raise UnsupportedAction("该驱动暂不支持获取群列表")

    async def get_group(self, group_id: str) -> Group:
        for group in await self.get_group_list():
            if group.group_id == group_id:
                return group
        raise DriverError(f"找不到群 {group_id}")

    async def get_group_members(self, group_id: str) -> List[GroupMember]:
        group = await self.get_group(group_id)
        return group.members

    async def get_group_member(self, group_id: str, user_id: str) -> GroupMember:
        for member in await self.get_group_members(group_id):
            if member.user_id == user_id:
                return member
        raise DriverError(f"群 {group_id} 中找不到成员 {user_id}")

    # -- 群管理 ------------------------------------------------------------
    async def set_group_card(self, group_id: str, user_id: str, card: str) -> None:
        raise UnsupportedAction("该驱动暂不支持修改群名片")

    async def set_group_ban(self, group_id: str, user_id: str, duration: int) -> None:
        raise UnsupportedAction("该驱动暂不支持禁言")

    async def set_group_whole_ban(self, group_id: str, enable: bool) -> None:
        raise UnsupportedAction("该驱动暂不支持全员禁言")

    async def set_group_kick(self, group_id: str, user_id: str, reject_add: bool = False) -> None:
        raise UnsupportedAction("该驱动暂不支持踢人")

    async def set_group_leave(self, group_id: str) -> None:
        raise UnsupportedAction("该驱动暂不支持退群")

    async def set_group_name(self, group_id: str, name: str) -> None:
        raise UnsupportedAction("该驱动暂不支持修改群名")

    async def set_group_admin(self, group_id: str, user_id: str, enable: bool) -> None:
        raise UnsupportedAction("该驱动暂不支持设置管理员")

    # -- 请求处理 ----------------------------------------------------------
    async def handle_friend_request(self, flag: str, approve: bool, remark: str = "") -> None:
        raise UnsupportedAction("该驱动暂不支持处理好友请求")

    async def handle_group_request(self, flag: str, sub_type: str, approve: bool, reason: str = "") -> None:
        raise UnsupportedAction("该驱动暂不支持处理群请求")

    # -- 登录（仅 supports_login 的驱动实现） ------------------------------
    def login_state(self) -> Dict[str, Any]:
        return {
            "supported": bool(self.supports_login),
            "logged_in": not self.supports_login,
            "account": {"user_id": "", "nickname": "", "avatar": ""},
            "qr": None,
            "message": "" if self.supports_login else "该驱动无需登录",
        }

    async def create_login_qr(self) -> Dict[str, Any]:
        raise UnsupportedAction(f"驱动 {self.name} 不支持扫码登录")

    async def check_login_qr(self, qr_uuid: str = "") -> Dict[str, Any]:
        raise UnsupportedAction(f"驱动 {self.name} 不支持扫码登录")

    async def logout(self) -> Dict[str, Any]:
        raise UnsupportedAction(f"驱动 {self.name} 不支持退出登录")

    # -- 状态 --------------------------------------------------------------
    async def status(self) -> Dict[str, Any]:
        return {"driver": self.name, "capabilities": sorted(self.capabilities)}

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def require(self, capability: str) -> None:
        if not self.supports(capability):
            raise UnsupportedAction(f"驱动 {self.name} 不支持 {capability}")
