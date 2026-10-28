"""iPad 协议登录：二维码获取 / 轮询、心跳、二次登录、退出登录。

这些接口在 `ipad.swagger.yml` 的 Login 分组里：
    POST /api/Login/LoginGetQR        body: {DeviceID, DeviceName, LoginType, Proxy}
    POST /api/Login/LoginCheckQR      query: uuid
    POST /api/Login/HeartBeat         query: wxid
    POST /api/Login/LoginTwiceAutoAuth query: wxid
    POST /api/Login/LogOut            query: wxid

`LoginCheckQR` 的 Data 在不同版本里可能是：
  * 字符串 ticket（表示已扫码、等待确认）
  * 对象 `{status, headImgUrl, nickName, ticket, acctSectResp:{userName, nickName, fsurl}}`
  * 登录成功后 `acctSectResp.userName` 才是 wxid，因此以它作为“成功”的判据。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuid_lib
from typing import Any, Dict, Optional

from ..model import DriverError

USER_AGENT = "wechat-onebot/1.0"

# 同一套协议服务支持多种登录端；不同端的伪装版本不同，旧端可能被服务端判定“版本过低”
LOGIN_PATHS: Dict[str, str] = {
    "ipad": "/Login/LoginGetQR",
    "ipadx": "/Login/LoginGetQRx",
    "pad": "/Login/LoginGetQRPad",
    "padx": "/Login/LoginGetQRPadx",
    "win": "/Login/LoginGetQRWin",
    "win_uwp": "/Login/LoginGetQRWinUwp",
    "win_unified": "/Login/LoginGetQRWinUnified",
    "mac": "/Login/LoginGetQRMac",
}

LOGIN_TYPES: Dict[str, str] = {
    "ipad": "ipad",
    "ipadx": "ipad",
    "pad": "pad",
    "padx": "pad",
    "win": "windows",
    "win_uwp": "windows",
    "win_unified": "windows",
    "mac": "mac",
}


def clean_service_message(message: str) -> str:
    """把服务端返回的 XML 提示压成一句话，方便直接显示在 WebUI。"""
    text = str(message or "").strip()
    match = re.search(r"<!\[CDATA\[(.*?)\]\]>", text, re.S)
    if match:
        text = match.group(1).strip()
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip() or "协议服务返回错误"


def _str(value: Any) -> str:
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
    return ""


def _pick(data: Any, *keys: str, default: Any = "") -> Any:
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


class IpadHttp:
    """极简 iPad 协议 HTTP 客户端（协议服务没有鉴权头，靠 wxid 区分账号）。"""

    def __init__(self, base_url: str, api_prefix: str = "/api", timeout: float = 20.0, wxid: str = "") -> None:
        self.base = str(base_url or "http://127.0.0.1:3010").rstrip("/") + str(api_prefix or "").rstrip("/")
        self.timeout = float(timeout or 20)
        self.wxid = str(wxid or "")

    async def post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, str]] = None,
        inject_wxid: bool = True,
    ) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        payload = dict(body or {})
        if inject_wxid and self.wxid and "Wxid" not in payload:
            payload["Wxid"] = self.wxid
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def _do() -> bytes:
            request = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()

        try:
            raw = await asyncio.to_thread(_do)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise DriverError(f"{path} HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise DriverError(f"{path} 请求失败: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriverError(f"{path} 返回非 JSON: {raw[:200]!r}") from exc
        if not isinstance(parsed, dict):
            raise DriverError(f"{path} 返回格式异常: {parsed!r}")
        try:
            code = int(parsed.get("Code") or 0)
        except (TypeError, ValueError):
            code = 0
        # 不同版本成功码不一致（v3.1.1 用 Code=1，v3.2 用 Code=0），以 Success 为准
        success = parsed.get("Success")
        if success is False or (success is None and code != 0):
            message = parsed.get("Message") or parsed.get("Msg") or f"Code {code}"
            raise DriverError(f"{path} 失败: {clean_service_message(message)}")
        return parsed.get("Data")


class IpadLoginClient:
    def __init__(
        self,
        http: IpadHttp,
        device_id: str = "",
        device_name: str = "wechat-onebot",
        login_type: str = "ipad",
        login_mode: str = "ipad",
    ) -> None:
        self.http = http
        self.device_id = device_id or uuid_lib.uuid4().hex
        self.device_name = device_name or "wechat-onebot"
        self.login_mode = str(login_mode or "ipad").strip().lower()
        if self.login_mode not in LOGIN_PATHS:
            self.login_mode = "ipad"
        self.login_type = login_type or LOGIN_TYPES.get(self.login_mode, "ipad")
        self._last_qr: Dict[str, Any] = {}

    # -- 二维码 ------------------------------------------------------------
    async def get_qr(self) -> Dict[str, Any]:
        data = await self.http.post(
            LOGIN_PATHS.get(self.login_mode, LOGIN_PATHS["ipad"]),
            {
                "DeviceID": self.device_id,
                "DeviceName": self.device_name,
                "LoginType": self.login_type,
                "Proxy": {},
            },
            inject_wxid=False,
        )
        qr = normalize_qr(data)
        self._last_qr = qr
        return qr

    async def check_qr(self, qr_uuid: str) -> Dict[str, Any]:
        qr_uuid = str(qr_uuid or "").strip()
        if not qr_uuid:
            raise DriverError("缺少二维码 uuid")
        try:
            data = await self.http.post("/Login/LoginCheckQR", query={"uuid": qr_uuid}, inject_wxid=False)
        except DriverError as exc:
            if is_expired_error(str(exc)):
                return {"stage": "expired", "uuid": qr_uuid, "message": "二维码已过期，请刷新"}
            raise
        result = normalize_check(data, qr_uuid)
        return result

    # -- 账号 --------------------------------------------------------------
    async def profile(self, wxid: str) -> Dict[str, str]:
        data = await self.http.post("/User/GetContractProfile", query={"wxid": wxid}, inject_wxid=False)
        return normalize_profile(data)

    async def heartbeat(self, wxid: str) -> None:
        await self.http.post("/Login/HeartBeat", query={"wxid": wxid}, inject_wxid=False)

    async def twice_auto_auth(self, wxid: str) -> None:
        await self.http.post("/Login/LoginTwiceAutoAuth", query={"wxid": wxid}, inject_wxid=False)

    async def awaken(self, wxid: str) -> None:
        await self.http.post("/Login/LoginAwaken", query={"wxid": wxid}, inject_wxid=False)

    async def logout(self, wxid: str) -> None:
        await self.http.post("/Login/LogOut", query={"wxid": wxid}, inject_wxid=False)


# ---------------------------------------------------------------------------
# 响应归一化
# ---------------------------------------------------------------------------


def is_expired_error(message: str) -> bool:
    text = (message or "").lower()
    return any(word in text for word in ("过期", "expire", "expired", "超时", "失效"))


def normalize_qr(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise DriverError(f"获取二维码返回格式异常: {data!r}")
    qr_uuid = str(_pick(data, "Uuid", "uuid", "UUID", "UuidStr") or "")
    if not qr_uuid:
        raise DriverError("协议服务没有返回 Uuid，无法轮询登录状态")
    image = str(
        _pick(data, "QRCodeBase64", "qrCodeBase64", "QrBase64", "Base64", "Image", "image") or ""
    )
    if image and not image.startswith("data:"):
        image = "data:image/png;base64," + image
    url = str(_pick(data, "QRCodeURL", "qrCodeUrl", "QrCodeUrl", "QrUrl", "Url", "url") or "")
    now = int(time.time())
    expires_raw = _pick(data, "ExpiredTime", "expireTime", "Expired", "expires", default=120)
    expires = 120.0
    if isinstance(expires_raw, str) and ("-" in expires_raw or ":" in expires_raw):
        # v3.1.1 返回的是日期字符串，例如 2026-10-28 18:10:19
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed_dt = time.mktime(time.strptime(expires_raw.strip(), fmt))
                expires = max(120.0, parsed_dt - now)
                break
            except (ValueError, OverflowError):
                continue
    else:
        try:
            expires = float(expires_raw)
        except (TypeError, ValueError):
            expires = 120.0
    if expires > 10 ** 12:  # 毫秒时间戳
        expires_at = int(expires / 1000)
    elif expires > 10 ** 9:  # 秒时间戳
        expires_at = int(expires)
    else:
        expires_at = now + max(120, int(expires or 120))
    # v3.1.x 返回 QrBase64/QrUrl，v3.2.x 返回 QRCodeBase64/QRCodeURL
    if "QrBase64" in data or ("QrUrl" in data and "QRCodeBase64" not in data):
        flavor = "legacy"
    elif "QRCodeBase64" in data or "QRCodeURL" in data:
        flavor = "v3"
    else:
        flavor = "unknown"
    return {
        "uuid": qr_uuid,
        "image": image,
        "url": url,
        "expires_at": expires_at,
        "created_at": now,
        "data62": str(_pick(data, "Data62", "data62") or ""),
        "flavor": flavor,
    }


def normalize_check(data: Any, qr_uuid: str) -> Dict[str, Any]:
    now = int(time.time())
    if data is None:
        return {"stage": "waiting", "uuid": qr_uuid, "message": "等待扫码"}
    if isinstance(data, str):
        # 协议有时候直接返回 ticket，表示“已扫码，等待手机确认”
        ticket = data.strip()
        if ticket:
            return {"stage": "scanned", "uuid": qr_uuid, "ticket": ticket, "message": "已扫码，请在手机上确认"}
        return {"stage": "waiting", "uuid": qr_uuid, "message": "等待扫码"}
    if not isinstance(data, dict):
        return {"stage": "waiting", "uuid": qr_uuid, "message": "等待扫码"}

    acct = data.get("acctSectResp") or data.get("AcctSectResp") or {}
    if not isinstance(acct, dict):
        acct = {}
    wxid = str(_pick(acct, "userName", "UserName", "username", "wxid") or _pick(data, "userName", "wxid", "username") or "")
    nickname = str(
        _pick(acct, "nickName", "NickName", "nickname") or _pick(data, "nickName", "nickname", "NickName") or ""
    )
    avatar = str(_pick(data, "headImgUrl", "headImgURL", "HeadImgUrl") or _pick(acct, "fsurl", "FsUrl") or "")
    ticket = str(_pick(data, "ticket", "Ticket") or "")
    status = data.get("status", data.get("Status"))
    expires_raw = _pick(data, "expiredTime", "ExpiredTime", "expireTime", default=None)
    expires_at = None
    if expires_raw not in (None, ""):
        try:
            value = float(expires_raw)
            expires_at = int(value) if value > 10 ** 9 else now + max(0, int(value))
        except (TypeError, ValueError):
            expires_at = None

    if wxid:
        return {
            "stage": "success",
            "uuid": qr_uuid,
            "ticket": ticket,
            "message": "登录成功",
            "account": {"user_id": wxid, "nickname": nickname or wxid, "avatar": avatar},
            "status": status,
            "expires_at": expires_at,
        }
    if ticket or (isinstance(status, int) and status > 0):
        return {
            "stage": "scanned",
            "uuid": qr_uuid,
            "ticket": ticket,
            "message": "已扫码，请在手机上确认" if ticket else "等待手机确认",
            "account": {"user_id": "", "nickname": nickname, "avatar": avatar},
            "status": status,
            "expires_at": expires_at,
        }
    return {
        "stage": "waiting",
        "uuid": qr_uuid,
        "message": "等待扫码",
        "status": status,
        "expires_at": expires_at,
    }


def normalize_profile(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        return {"user_id": "", "nickname": "", "avatar": ""}
    user = data.get("userInfo") or data.get("UserInfo") or {}
    ext = data.get("userInfoExt") or data.get("UserInfoExt") or {}
    if not isinstance(user, dict):
        user = {}
    if not isinstance(ext, dict):
        ext = {}
    return {
        "user_id": str(_str(_pick(user, "UserName", "userName")) or ""),
        "nickname": str(_str(_pick(user, "NickName", "nickName", "nickname")) or ""),
        "avatar": str(
            _str(_pick(ext, "BigHeadImgUrl", "bigHeadImgUrl", "HeadImgUrl"))
            or _str(_pick(user, "BigHeadImgUrl", "bigHeadImgUrl"))
            or ""
        ),
    }


def qr_png_bytes(qr: Dict[str, Any]) -> Optional[bytes]:
    """把二维码 data URI 解成 PNG 字节，供命令行保存/打开。"""
    image = str(qr.get("image") or "")
    if not image:
        return None
    if image.startswith("data:"):
        try:
            return base64.b64decode(image.split(",", 1)[1])
        except Exception:
            return None
    try:
        return base64.b64decode(image)
    except Exception:
        return None
