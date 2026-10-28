"""iPad 协议驱动：对接 iPad 微信协议服务（例如 wechat-robot-client 使用的那套 HTTP 服务）。

工作方式：
  * 出方向：调用 `POST {base_url}/api/Msg/SendTxt` 等接口发消息
  * 入方向：协议服务把 AddMsg 回调 POST 到本程序（默认 9002 端口）
      - 兼容 wechat-robot-client 的 `/api/v1/wechat-client/{wxid}/sync-message`
      - 也兼容 message_callback.md 里 `{TypeName:"AddMsg", Wxid, Data:{...}}` 的单条推送

注意：不同版本/厂商的 iPad 协议字段略有差异，这里对 `SKBuiltinStringT` 之类的包装做了兼容；
如果某个接口在你的服务端上失败，日志里会带上 HTTP 状态与原始返回，方便定位。
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from ..httpkit import RawRequest, json_response
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
    UnsupportedAction,
)
from .base import BaseDriver
from .ipad_login import IpadHttp, IpadLoginClient, is_expired_error

GROUP_SUFFIX = "@chatroom"
SPECIAL_CONTACTS = {"weixin", "filehelper", "fmessage", "medianote", "floatbottle", "newsapp", "notifymessage"}
TEXT_PREFIX = re.compile(r"^[A-Za-z0-9_@.\-]{2,64}:\n")


def _str(value: Any) -> str:
    """兼容微信的 SKBuiltinStringT：`{"string": "x"}` 或直接字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("string", "String", "str", "value"):
            if key in value:
                return _str(value[key])
        # 某些版本会嵌套 {"iLen":.., "buffer":..}
    return ""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class IpadDriver(BaseDriver):
    name = "ipad"
    supports_login = True

    def __init__(self, config: Dict[str, Any], ctx: Any) -> None:
        super().__init__(config, ctx)
        self.capabilities = {"text", "image", "at", "revoke", "group_manage"}
        base = str(config.get("base_url") or "http://127.0.0.1:3010").rstrip("/")
        prefix = str(config.get("api_prefix") or "/api").rstrip("/")
        self.base = base + prefix
        self.timeout = float(config.get("timeout") or 20)
        account = ctx.config.account() if ctx else {}
        self.wxid = str(config.get("wxid") or account.get("wxid") or "")
        self.nickname = str(account.get("nickname") or "")
        self.callback_prefix = str(config.get("callback_path_prefix") or "/api/v1/wechat-client")
        self.contacts_ttl = int(config.get("refresh_contacts_sec") or 300)
        self.max_contact_batches = int(config.get("max_contact_batches") or 10)
        self._contacts: List[Dict[str, Any]] = []
        self._contacts_at = 0.0
        self._members: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

        # 登录相关
        login_cfg = config.get("login") or {}
        self.login_mode = str(config.get("login_mode") or "ipad").strip().lower()
        self.http = IpadHttp(base, prefix, self.timeout, self.wxid)
        self.login_client = IpadLoginClient(
            self.http,
            device_id=str(login_cfg.get("device_id") or ""),
            device_name=str(login_cfg.get("device_name") or "wechat-onebot"),
            login_type=str(login_cfg.get("login_type") or ""),
            login_mode=self.login_mode,
        )
        self.logged_in = False
        self.account: Dict[str, str] = {"user_id": self.wxid, "nickname": self.nickname, "avatar": ""}
        self._qr: Optional[Dict[str, Any]] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.protocol_flavor = "unknown"
        self._service_ok = True

    # -- 生命周期 ----------------------------------------------------------
    async def start(self) -> None:
        await super().start()
        prefix = self.callback_prefix.rstrip("/")
        self.ctx.register_route("POST", f"{prefix}/{{wxid}}/sync-message", self._on_sync_message)
        self.ctx.register_route("POST", f"{prefix}/{{wxid}}/logout", self._on_logout)
        self.ctx.register_route("POST", "/callback", self._on_callback)
        self.ctx.register_route("POST", "/wechat/callback", self._on_callback)
        self.ctx.register_route("POST", "/", self._on_callback)
        self._service_ok = await self._probe_service()
        if self._service_ok:
            self.log.info("iPad 协议服务可用：%s", self.http.base)
        else:
            self.log.warning("连接 iPad 协议服务失败（%s）", self.http.base)
            self.log.warning("请确认协议服务已启动，且 wechat.json -> ipad.base_url 指向正确")

    async def stop(self) -> None:
        self._stop_heartbeat()
        await super().stop()

    async def _probe_service(self) -> bool:
        parsed = urllib.parse.urlsplit(self.http.base)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.5)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    async def login(self) -> LoginInfo:
        login_cfg = self.config.get("login") or {}
        auto_login = bool(login_cfg.get("auto_login", True))
        self.account.update({"user_id": self.wxid, "nickname": self.nickname})
        if not self.wxid:
            self.log.info("尚未登录：打开 WebUI「微信登录」扫码，或运行 ./start.sh --qr")
            return LoginInfo("", self.nickname or "微信")
        if not auto_login:
            self.log.info("auto_login 已关闭，请在 WebUI「微信登录」扫码")
            return LoginInfo(self.wxid, self.nickname or self.wxid)
        try:
            await self.login_client.twice_auto_auth(self.wxid)
        except Exception as exc:
            self.log.debug("二次登录接口不可用：%s", exc)
        try:
            await self.login_client.heartbeat(self.wxid)
            self._service_ok = True
            self.logged_in = True
            try:
                self._apply_profile(await self.login_client.profile(self.wxid))
            except DriverError as exc:
                self.log.debug("读取账号资料失败（不影响收发）：%s", exc)
            self._start_heartbeat()
            self.log.info("自动登录成功：%s(%s)", self.nickname, self.wxid)
        except DriverError as exc:
            self.logged_in = False
            self.log.info("自动登录未成功（%s），可在 WebUI「微信登录」扫码", exc)
        return LoginInfo(self.wxid, self.nickname or self.wxid)

    # -- 扫码登录 ----------------------------------------------------------
    def login_state(self) -> Dict[str, Any]:
        login_cfg = self.config.get("login") or {}
        return {
            "supported": True,
            "logged_in": self.logged_in,
            "account": {
                "user_id": self.wxid,
                "nickname": self.nickname,
                "avatar": self.account.get("avatar", ""),
            },
            "qr": dict(self._qr) if (self._qr and not self.logged_in) else None,
            "auto_login": bool(login_cfg.get("auto_login", True)),
            "heartbeat": bool(self._heartbeat_task and not self._heartbeat_task.done()),
            "service": self.http.base,
            "protocol": self.protocol_flavor,
            "service_ok": self._service_ok,
            "login_mode": self.login_mode,
        }

    async def create_login_qr(self) -> Dict[str, Any]:
        if self.logged_in:
            raise DriverError("当前已登录，如需切换账号请先退出登录")
        try:
            qr = await self.login_client.get_qr()
        except Exception:
            self._service_ok = False
            raise
        self._service_ok = True
        self._qr = qr
        self.protocol_flavor = str(qr.get("flavor") or "unknown")
        remain = max(0, int(qr.get("expires_at") or 0) - int(time.time()))
        self.log.info("已生成登录二维码（uuid=%s，约 %s 秒内有效）", qr.get("uuid"), remain)
        return qr

    async def check_login_qr(self, qr_uuid: str = "") -> Dict[str, Any]:
        qr_uuid = str(qr_uuid or "").strip() or str((self._qr or {}).get("uuid") or "")
        if not qr_uuid:
            raise DriverError("请先获取登录二维码")
        result = await self.login_client.check_qr(qr_uuid)
        if result.get("expires_at") and self._qr is not None:
            self._qr["expires_at"] = int(result["expires_at"])
        if self.protocol_flavor == "unknown" and result.get("account"):
            self.protocol_flavor = "legacy" if result.get("uuid") else "unknown"
        if result.get("stage") == "success":
            await self._on_login_success(result.get("account") or {})
            result["state"] = self.login_state()
        elif result.get("stage") == "expired":
            self._qr = None
        return result

    async def _on_login_success(self, account: Dict[str, Any]) -> None:
        wxid = str(account.get("user_id") or "")
        if not wxid:
            return
        self.wxid = wxid
        self.http.wxid = wxid
        self.nickname = str(account.get("nickname") or self.nickname or wxid)
        self.account = {
            "user_id": wxid,
            "nickname": self.nickname,
            "avatar": str(account.get("avatar") or ""),
        }
        self.logged_in = True
        self._qr = None
        self._contacts = []
        self._contacts_at = 0.0
        self._members.clear()
        try:
            self._apply_profile(await self.login_client.profile(wxid))
        except DriverError as exc:
            self.log.debug("登录后读取资料失败：%s", exc)
        self.log.info("微信登录成功：%s(%s)", self.nickname, self.wxid)
        try:
            await self.ctx.emit_login(LoginInfo(self.wxid, self.nickname))
        except Exception as exc:
            self.log.debug("通知网关登录状态失败：%s", exc)
        self._start_heartbeat()

    def _apply_profile(self, profile: Dict[str, str]) -> None:
        if not isinstance(profile, dict):
            return
        if profile.get("user_id"):
            self.wxid = str(profile["user_id"])
            self.http.wxid = self.wxid
        if profile.get("nickname"):
            self.nickname = str(profile["nickname"])
        self.account.update(
            user_id=self.wxid,
            nickname=self.nickname,
            avatar=str(profile.get("avatar") or self.account.get("avatar", "")),
        )

    async def logout(self) -> Dict[str, Any]:
        wxid = self.wxid
        if wxid:
            try:
                await self.login_client.logout(wxid)
            except DriverError as exc:
                self.log.warning("调用退出登录接口失败（本地状态仍会清除）：%s", exc)
        self.logged_in = False
        self._qr = None
        self._stop_heartbeat()
        self.log.info("已退出微信登录")
        return self.login_state()

    # -- 心跳 --------------------------------------------------------------
    def _start_heartbeat(self) -> None:
        if not self.logged_in or not self.wxid:
            return
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        login_cfg = self.config.get("login") or {}
        interval = max(10, int(login_cfg.get("heartbeat_sec") or 25))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self, interval: int) -> None:
        failures = 0
        while self._started and self.logged_in:
            await asyncio.sleep(interval)
            if not self.logged_in:
                return
            try:
                await self.login_client.heartbeat(self.wxid)
                self._service_ok = True
                if failures:
                    self.log.info("微信心跳已恢复")
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.log.warning("微信心跳失败 %s/3：%s", failures, exc)
                if failures >= 2:
                    self._service_ok = False
                if failures >= 3:
                    self.logged_in = False
                    self.log.warning("连续心跳失败，已标记为未登录，请在 WebUI 重新扫码")
                    return

    # -- HTTP 基础 ---------------------------------------------------------
    async def _post(self, path: str, body: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, str]] = None) -> Any:
        return await self.http.post(path, body, query)

    # -- 联系人 / 群 -------------------------------------------------------
    async def _ensure_contacts(self, force: bool = False) -> List[Dict[str, Any]]:
        if not force and self._contacts and time.time() - self._contacts_at < self.contacts_ttl:
            return self._contacts
        async with self._lock:
            if not force and self._contacts and time.time() - self._contacts_at < self.contacts_ttl:
                return self._contacts
            data = await self._post(
                "/Friend/GetContractList",
                {"CurrentWxcontactSeq": 0, "CurrentChatRoomContactSeq": 0},
            )
            usernames: List[str] = []
            if isinstance(data, dict):
                usernames = [str(x) for x in (data.get("ContactUsernameList") or []) if x]
            details: List[Dict[str, Any]] = []
            for index in range(0, len(usernames), 20):
                if index // 20 >= self.max_contact_batches:
                    self.log.warning("联系人过多，仅拉取前 %s 个详情", self.max_contact_batches * 20)
                    break
                batch = usernames[index : index + 20]
                resp = await self._post(
                    "/Friend/GetContractDetail",
                    {"Towxids": ",".join(batch), "ChatRoom": ""},
                )
                if isinstance(resp, dict):
                    for item in resp.get("ContactList") or []:
                        if isinstance(item, dict):
                            details.append(item)
            self._contacts = details
            self._contacts_at = time.time()
            self.log.info("通讯录同步完成：%s 个联系人", len(details))
            return self._contacts

    def _is_group_contact(self, contact: Dict[str, Any]) -> bool:
        return _str(contact.get("UserName")).endswith(GROUP_SUFFIX)

    def _is_friend_contact(self, contact: Dict[str, Any]) -> bool:
        username = _str(contact.get("UserName"))
        if not username or username.endswith(GROUP_SUFFIX):
            return False
        if username in SPECIAL_CONTACTS or username.startswith("gh_"):
            return False
        return _int(contact.get("DeleteFlag"), 0) == 0

    async def get_friend_list(self) -> List[Friend]:
        contacts = await self._ensure_contacts()
        friends: List[Friend] = []
        for contact in contacts:
            if not self._is_friend_contact(contact):
                continue
            username = _str(contact.get("UserName"))
            friends.append(
                Friend(
                    user_id=username,
                    nickname=_str(contact.get("NickName")) or username,
                    remark=_str(contact.get("Remark")),
                    avatar=_str(contact.get("BigHeadImgUrl")) or _str(contact.get("SmallHeadImgUrl")),
                )
            )
        if self.wxid and not any(f.user_id == self.wxid for f in friends):
            friends.insert(0, Friend(self.wxid, self.nickname or self.wxid))
        return friends

    async def get_friend(self, user_id: str) -> Friend:
        for friend in await self.get_friend_list():
            if friend.user_id == user_id:
                return friend
        return Friend(user_id, user_id)

    async def get_group_list(self) -> List[Group]:
        contacts = await self._ensure_contacts()
        groups: List[Group] = []
        for contact in contacts:
            if not self._is_group_contact(contact):
                continue
            group_id = _str(contact.get("UserName"))
            owner = _str(contact.get("ChatRoomOwner"))
            groups.append(
                Group(
                    group_id=group_id,
                    name=_str(contact.get("NickName")) or group_id,
                    owner_id=owner,
                    members=[],
                )
            )
        return groups

    async def get_group(self, group_id: str) -> Group:
        for group in await self.get_group_list():
            if group.group_id == str(group_id):
                return group
        return Group(str(group_id), str(group_id))

    async def _fetch_members(self, group_id: str) -> List[Dict[str, Any]]:
        data = await self._post("/Group/GetChatRoomMemberDetail", {"QID": str(group_id)})
        members: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            new_data = data.get("NewChatroomData") or data.get("newChatroomData") or {}
            members = list((new_data or {}).get("ChatRoomMember") or [])
        return [item for item in members if isinstance(item, dict)]

    async def get_group_members(self, group_id: str) -> List[GroupMember]:
        cached = self._members.get(str(group_id))
        if cached and time.time() - cached[0] < self.contacts_ttl:
            raw_members = cached[1]
        else:
            raw_members = await self._fetch_members(group_id)
            self._members[str(group_id)] = (time.time(), raw_members)
        group = await self.get_group(group_id)
        result: List[GroupMember] = []
        for item in raw_members:
            username = _str(item.get("UserName"))
            if not username:
                continue
            nickname = _str(item.get("NickName")) or username
            card = _str(item.get("DisplayName"))
            role = "member"
            if group.owner_id and username == group.owner_id:
                role = "owner"
            else:
                flag = _int(item.get("ChatroomMemberFlag"), 0)
                if flag & 2:
                    role = "admin"
                elif flag & 1:
                    role = "owner"
            result.append(GroupMember(user_id=username, nickname=nickname, card=card, role=role))
        return result

    async def get_group_member(self, group_id: str, user_id: str) -> GroupMember:
        for member in await self.get_group_members(group_id):
            if member.user_id == user_id:
                return member
        return GroupMember(user_id, user_id)

    # -- 发送 --------------------------------------------------------------
    async def send_message(self, chat_type: str, chat_id: str, segments: List[Segment]) -> Tuple[Optional[str], int]:
        text, at_ids, media = self._split(segments)
        if chat_type == "group" and not chat_id:
            raise DriverError("缺少群 ID")
        platform_id: Optional[str] = None
        if media:
            for kind, data, filename in media:
                if kind == "image":
                    platform_id = await self._send_image(chat_id, data, filename)
                else:
                    raise UnsupportedAction(f"iPad 驱动暂不支持发送 {kind}（仅支持文本与图片）")
        if text:
            new_id = await self._send_text(chat_id, text, at_ids)
            platform_id = platform_id or new_id
        if platform_id is None and not text and not media:
            raise DriverError("消息为空")
        return platform_id, 0

    def _split(self, segments: List[Segment]) -> Tuple[str, List[str], List[Tuple[str, Segment, str]]]:
        texts: List[str] = []
        at_ids: List[str] = []
        media: List[Tuple[str, Segment, str]] = []
        for seg in segments:
            if seg.type == "text":
                texts.append(str(seg.data.get("text", "")))
            elif seg.type == "at":
                qq = str(seg.data.get("qq", "")).strip()
                if qq:
                    at_ids.append("notify@all" if qq == "all" else qq)
                    if qq != "all":
                        name = seg.data.get("name") or qq
                        texts.append(f"@{name}")
            elif seg.type == "image":
                media.append(("image", seg, str(seg.data.get("file") or seg.data.get("url") or "")))
            elif seg.type in ("record", "audio"):
                media.append(("record", seg, str(seg.data.get("file") or seg.data.get("url") or "")))
            elif seg.type == "video":
                media.append(("video", seg, str(seg.data.get("file") or seg.data.get("url") or "")))
            elif seg.type == "file":
                media.append(("file", seg, str(seg.data.get("file") or "")))
            elif seg.type == "face":
                texts.append("[表情]")
            elif seg.type == "reply":
                continue
        text = "".join(texts).strip()
        return text, at_ids, media

    async def _send_text(self, chat_id: str, text: str, at_ids: List[str]) -> Optional[str]:
        body = {
            "ToWxid": str(chat_id),
            "Content": text,
            "Type": 1,
            "At": ",".join(at_ids),
        }
        data = await self._post("/Msg/SendTxt", body)
        new_id = ""
        if isinstance(data, dict):
            items = data.get("List") or []
            if items and isinstance(items[0], dict):
                new_id = str(items[0].get("NewMsgId") or items[0].get("MsgId") or "")
            if not new_id:
                new_id = str(data.get("NewMsgId") or data.get("MsgId") or "")
        return new_id or None

    async def _send_image(self, chat_id: str, segment: Segment, source: str) -> Optional[str]:
        data = await self._load_media(segment, source)
        encoded = base64.b64encode(data).decode("ascii")
        resp = await self._post("/Msg/UploadImg", {"ToWxid": str(chat_id), "Base64": encoded})
        if isinstance(resp, dict):
            return str(resp.get("Newmsgid") or resp.get("NewMsgId") or resp.get("Msgid") or "") or None
        return None

    async def _load_media(self, segment: Segment, source: str) -> bytes:
        source = source or str(segment.data.get("url") or "")
        if not source:
            raise DriverError("图片缺少 file/url")
        if source.startswith("base64://"):
            return base64.b64decode(source[len("base64://") :])
        if source.startswith("data:"):
            try:
                return base64.b64decode(source.split(",", 1)[1])
            except Exception as exc:
                raise DriverError(f"data URI 解析失败: {exc}") from exc
        if source.startswith("file://"):
            from pathlib import Path
            from urllib.parse import unquote, urlparse

            return Path(unquote(urlparse(source).path)).read_bytes()
        if source.startswith("http://") or source.startswith("https://"):
            def _download() -> bytes:
                request = urllib.request.Request(source, headers={"User-Agent": "wechat-onebot/1.0"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()

            try:
                data = await asyncio.to_thread(_download)
            except Exception as exc:
                raise DriverError(f"下载图片失败 {source}: {exc}") from exc
            if len(data) > 20 * 1024 * 1024:
                raise DriverError("图片超过 20MB，iPad 协议可能拒绝")
            return data
        from pathlib import Path

        path = Path(source)
        if path.is_file():
            return path.read_bytes()
        raise DriverError(f"无法识别的图片来源: {source[:80]}")

    async def revoke(self, chat_type: str, chat_id: str, platform_message_id: str) -> None:
        await self._post(
            "/Msg/Revoke",
            {
                "ToUserName": str(chat_id),
                "NewMsgId": _int(platform_message_id),
                "ClientMsgId": 0,
                "CreateTime": int(time.time()),
            },
        )

    async def status(self) -> Dict[str, Any]:
        base = await super().status()
        base["logged_in"] = self.logged_in
        base["login"] = self.login_state()
        return base

    # -- 群管理 ------------------------------------------------------------
    async def set_group_kick(self, group_id: str, user_id: str, reject_add: bool = False) -> None:
        await self._post("/Group/DelChatRoomMember", {"ChatRoomName": str(group_id), "ToWxids": str(user_id)})

    async def set_group_name(self, group_id: str, name: str) -> None:
        await self._post("/Group/SetChatRoomName", {"QID": str(group_id), "Content": name})

    async def set_group_admin(self, group_id: str, user_id: str, enable: bool) -> None:
        await self._post(
            "/Group/OperateChatRoomAdmin",
            {"QID": str(group_id), "ToWxids": str(user_id), "Val": 1 if enable else 0},
        )

    async def set_group_card(self, group_id: str, user_id: str, card: str) -> None:
        self.log.warning("iPad 协议未提供稳定的改群名片接口，已忽略：%s -> %s", user_id, card)

    async def set_group_ban(self, group_id: str, user_id: str, duration: int) -> None:
        raise UnsupportedAction("iPad 协议未开放禁言接口")

    async def set_group_whole_ban(self, group_id: str, enable: bool) -> None:
        raise UnsupportedAction("iPad 协议未开放全员禁言接口")

    async def set_group_leave(self, group_id: str) -> None:
        await self._post("/Group/Quit", {"QID": str(group_id)})

    # -- 回调 --------------------------------------------------------------
    async def _on_logout(self, request: RawRequest):
        wxid = str(request.params.get("wxid") or "")
        self.log.warning("协议服务通知账号下线：%s", wxid or self.wxid)
        return json_response({"Success": True, "Code": 0, "Message": "", "Data": None})

    async def _on_sync_message(self, request: RawRequest):
        wxid = str(request.params.get("wxid") or "")
        body = request.json()
        if wxid and wxid != self.wxid:
            await self._learn_account(wxid)
        messages = []
        if isinstance(body, dict):
            data = body.get("Data")
            if isinstance(data, dict):
                messages = data.get("AddMsgs") or []
            elif isinstance(body.get("AddMsgs"), list):
                messages = body["AddMsgs"]
            elif isinstance(data, dict) and "MsgType" in data:
                messages = [data]
        for raw in messages:
            if isinstance(raw, dict):
                await self._handle_add_msg(raw)
        return json_response({"Success": True, "Code": 0, "Message": "", "Data": None})

    async def _on_callback(self, request: RawRequest):
        body = request.json()
        await self._handle_envelope(body)
        return json_response({"Success": True, "Code": 0, "Message": "", "Data": None})

    async def _handle_envelope(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        if body.get("TypeName") == "AddMsg" and isinstance(body.get("Data"), dict):
            wxid = str(body.get("Wxid") or "")
            if wxid and wxid != self.wxid:
                await self._learn_account(wxid)
            await self._handle_add_msg(body["Data"])
            return
        if isinstance(body.get("Data"), dict):
            data = body["Data"]
            if isinstance(data.get("AddMsgs"), list):
                wxid = str(body.get("Wxid") or "")
                if wxid and wxid != self.wxid:
                    await self._learn_account(wxid)
                for raw in data["AddMsgs"]:
                    if isinstance(raw, dict):
                        await self._handle_add_msg(raw)
                return
            if "MsgType" in data:
                await self._handle_add_msg(data)
                return
        if "MsgType" in body:
            await self._handle_add_msg(body)

    async def _learn_account(self, wxid: str) -> None:
        self.wxid = wxid
        self.http.wxid = wxid
        self.account["user_id"] = wxid
        if not self.logged_in:
            self.logged_in = True
            self._start_heartbeat()
        try:
            await self.ctx.emit_login(LoginInfo(wxid, self.nickname))
        except Exception:
            pass
        self.log.info("从回调学习到当前账号：%s", wxid)

    # -- 消息解析 ----------------------------------------------------------
    async def _handle_add_msg(self, raw: Dict[str, Any]) -> None:
        from_user = _str(raw.get("FromUserName"))
        to_user = _str(raw.get("ToUserName"))
        msg_type = _int(raw.get("MsgType"))
        content = _str(raw.get("Content"))
        new_msg_id = str(raw.get("NewMsgId") or raw.get("MsgId") or "")
        create_time = _int(raw.get("CreateTime"), int(time.time()))
        if not new_msg_id:
            new_msg_id = f"ipad-{create_time}-{_int(raw.get('MsgId'))}"
        if not self.wxid and from_user:
            await self._learn_account(from_user)

        is_self = bool(self.wxid) and from_user == self.wxid
        group_id = ""
        if to_user.endswith(GROUP_SUFFIX):
            group_id = to_user
        elif from_user.endswith(GROUP_SUFFIX):
            group_id = from_user
        chat_type = "group" if group_id else "private"
        chat_id = group_id or (to_user if is_self else from_user)
        if not chat_id:
            chat_id = from_user or to_user
        sender_id = self.wxid if is_self else from_user
        if chat_type == "private" and is_self:
            sender_id = self.wxid

        sender_name = await self._name_of(sender_id, chat_id, chat_type)
        sender = Sender(sender_id, sender_name, "", role="member")

        segments: List[Segment] = []
        quote_id: Optional[str] = None
        notice: Optional[Notice] = None

        if msg_type == 1:
            segments = await self._text_segments(content, raw, group_id, chat_type, sender_id)
        elif msg_type == 3:
            segments = self._image_segments(raw, content)
        elif msg_type == 34:
            segments = self._voice_segments(raw)
        elif msg_type in (43, 62):
            segments = self._video_segments(raw)
        elif msg_type == 42:
            segments = [Segment("text", {"text": "[名片] " + (content[:80] if content else "")})]
        elif msg_type == 47:
            segments = self._emoji_segments(raw, content)
        elif msg_type == 48:
            segments = [Segment("text", {"text": "[位置]"})]
        elif msg_type == 49:
            segments, quote_id = await self._app_segments(content, group_id, chat_type, sender_id)
        elif msg_type in (10000, 10002):
            notice = self._system_notice(content, group_id, chat_type, sender_id)
            segments = []
        elif msg_type == 51:
            return
        else:
            segments = [Segment("text", {"text": content[:200]})] if content else []

        if notice is not None:
            await self.ctx.emit_notice(notice)
            return
        if not segments:
            return

        message = ChatMessage(
            platform_id=new_msg_id,
            chat_type=chat_type,
            chat_id=chat_id,
            sender=sender,
            segments=segments,
            time=create_time,
            is_self=is_self,
            quote_platform_id=quote_id,
            raw=raw,
        )
        await self.ctx.emit_message(message)

    async def _name_of(self, user_id: str, chat_id: str, chat_type: str) -> str:
        if user_id == self.wxid and self.nickname:
            return self.nickname
        if chat_type == "group":
            cached = self._members.get(chat_id)
            if cached:
                for item in cached[1]:
                    if _str(item.get("UserName")) == user_id:
                        return _str(item.get("DisplayName")) or _str(item.get("NickName")) or user_id
        for contact in self._contacts:
            if _str(contact.get("UserName")) == user_id:
                return _str(contact.get("Remark")) or _str(contact.get("NickName")) or user_id
        return user_id

    def _at_list(self, raw: Dict[str, Any]) -> List[str]:
        source = _str(raw.get("MsgSource"))
        if not source:
            return []
        match = re.search(r"<atuserlist>(?:<!\[CDATA\[)?([^<\]]*)", source)
        if not match:
            return []
        return [item.strip() for item in match.group(1).split(",") if item.strip()]

    async def _text_segments(
        self, content: str, raw: Dict[str, Any], group_id: str, chat_type: str, sender_id: str
    ) -> List[Segment]:
        text = content
        if chat_type == "group":
            text = TEXT_PREFIX.sub("", text)
        elif chat_type == "private" and text.startswith("<"):
            return [Segment("text", {"text": text[:200]})]
        segments: List[Segment] = []
        at_ids = self._at_list(raw)
        # 微信文本里的 @昵称 与 atuserlist 对应，按出现顺序剥离
        for at_id in at_ids:
            name = ""
            if at_id == "notify@all":
                name = "全体成员"
            else:
                name = await self._name_of(at_id, group_id, chat_type)
            for pattern in (f"@{name}\u2005", f"@{name} ", f"@{name}"):
                if pattern and pattern in text:
                    text = text.replace(pattern, "", 1)
                    break
            segments.append(Segment("at", {"qq": "all" if at_id == "notify@all" else at_id, "name": name}))
        if text:
            segments.append(Segment("text", {"text": text}))
        return segments or [Segment("text", {"text": content})]

    def _image_segments(self, raw: Dict[str, Any], content: str) -> List[Segment]:
        buf = (raw.get("ImgBuf") or {}).get("buffer") if isinstance(raw.get("ImgBuf"), dict) else None
        if buf:
            try:
                data = base64.b64decode(buf)
                url = self.ctx.media_url(data, "image.jpg", "image/jpeg")
                if url:
                    return [Segment("image", {"file": url, "url": url, "summary": "[图片]"})]
            except Exception as exc:
                self.log.debug("图片解码失败：%s", exc)
        md5 = ""
        match = re.search(r'md5="([0-9a-f]+)"', content or "")
        if match:
            md5 = match.group(1)
        return [Segment("text", {"text": f"[图片] {md5}".strip()})]

    def _voice_segments(self, raw: Dict[str, Any]) -> List[Segment]:
        buf = (raw.get("ImgBuf") or {}).get("buffer") if isinstance(raw.get("ImgBuf"), dict) else None
        if buf:
            try:
                data = base64.b64decode(buf)
                url = self.ctx.media_url(data, "voice.silk", "audio/silk")
                if url:
                    return [Segment("record", {"file": url, "url": url, "summary": "[语音]"})]
            except Exception:
                pass
        return [Segment("text", {"text": "[语音]"})]

    def _video_segments(self, raw: Dict[str, Any]) -> List[Segment]:
        buf = (raw.get("ImgBuf") or {}).get("buffer") if isinstance(raw.get("ImgBuf"), dict) else None
        if buf:
            try:
                data = base64.b64decode(buf)
                url = self.ctx.media_url(data, "video.mp4", "video/mp4")
                if url:
                    return [Segment("video", {"file": url, "url": url, "summary": "[视频]"})]
            except Exception:
                pass
        return [Segment("text", {"text": "[视频]"})]

    def _emoji_segments(self, raw: Dict[str, Any], content: str) -> List[Segment]:
        buf = (raw.get("ImgBuf") or {}).get("buffer") if isinstance(raw.get("ImgBuf"), dict) else None
        if buf:
            try:
                url = self.ctx.media_url(base64.b64decode(buf), "emoji.gif", "image/gif")
                if url:
                    return [Segment("image", {"file": url, "url": url, "summary": "[表情]"})]
            except Exception:
                pass
        return [Segment("text", {"text": "[表情]"})]

    async def _app_segments(
        self, content: str, group_id: str, chat_type: str, sender_id: str
    ) -> Tuple[List[Segment], Optional[str]]:
        if not content or not content.lstrip().startswith("<"):
            return [Segment("text", {"text": content[:200]})], None
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return [Segment("text", {"text": "[链接/小程序]"})], None
        app = root.find("appmsg")
        if app is None:
            return [Segment("text", {"text": "[消息]"})], None
        app_type = _int(app.findtext("type"), 0)
        title = (app.findtext("title") or "").strip()
        url = (app.findtext("url") or "").strip()
        quote_id: Optional[str] = None
        if app_type == 57:
            refer = app.find("refermsg")
            if refer is not None:
                quote_id = (refer.findtext("svrid") or "").strip() or None
            return [Segment("text", {"text": title or "[引用消息]"})], quote_id
        if app_type == 6:
            attach = app.find("appattach")
            filename = (attach.findtext("filename") if attach is not None else "") or title or "文件"
            size = _int(attach.findtext("totallen") if attach is not None else 0)
            label = f"[文件] {filename}" + (f" ({size} B)" if size else "")
            return [Segment("text", {"text": label})], None
        if app_type == 5:
            text = f"{title}\n{url}".strip() if url else (title or "[链接]")
            return [Segment("text", {"text": text})], None
        if app_type in (19, 51):
            return [Segment("text", {"text": f"[聊天记录] {title}".strip()})], None
        if app_type in (2000, 2001):
            return [Segment("text", {"text": f"[转账/红包] {title}".strip()})], None
        if title:
            return [Segment("text", {"text": title})], None
        return [Segment("text", {"text": "[小程序/链接]"})], None

    def _system_notice(self, content: str, group_id: str, chat_type: str, sender_id: str) -> Optional[Notice]:
        if not content:
            return None
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return None
        sysmsg = root.find("sysmsg") if root.tag != "sysmsg" else root
        if sysmsg is None:
            return None
        kind_attr = (sysmsg.get("type") or "").lower()
        if sysmsg.find("revokemsg") is not None:
            revoke = sysmsg.find("revokemsg")
            new_msg_id = (revoke.findtext("newmsgid") or revoke.findtext("msgid") or "").strip()
            return Notice(
                kind="group_recall" if chat_type == "group" else "friend_recall",
                chat_type=chat_type,
                chat_id=group_id or sender_id,
                user_id=sender_id,
                operator_id=sender_id,
                message_id=new_msg_id,
            )
        pat = sysmsg.find("pat")
        if pat is not None:
            return Notice(
                kind="poke",
                chat_type=chat_type,
                chat_id=group_id or sender_id,
                user_id=(pat.findtext("fromusername") or sender_id).strip(),
                target_id=(pat.findtext("pattedusername") or "").strip(),
            )
        template = sysmsg.find("sysmsgtemplate")
        if template is not None:
            plain = "".join(template.itertext()).lower()
            notice_type = ""
            if "加入" in plain or "invite" in plain:
                notice_type = "group_increase"
            elif "移出" in plain or "踢" in plain or "leave" in plain:
                notice_type = "group_decrease"
            if notice_type and chat_type == "group":
                return Notice(
                    kind=notice_type,
                    chat_type=chat_type,
                    chat_id=group_id,
                    user_id=sender_id,
                    operator_id=sender_id,
                    extra={"sub_type": "approve"},
                )
        self.log.debug("未识别的系统消息：%s（type=%s）", content[:120], kind_attr)
        return None
