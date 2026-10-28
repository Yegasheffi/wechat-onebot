"""命令行入口：python3 -m wechat_onebot [--config-dir DIR] [--driver mock|ipad|bridge]"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .config import ConfigStore, parse_simple_args, resolve_config_dir
from .drivers.ipad_login import IpadHttp, IpadLoginClient, qr_png_bytes
from .gateway import Gateway, StartupError
from .logger import get_logger, set_level
from .model import DriverError
from .preflight import check as preflight_check, remove_pid, stop_instance, warnings as preflight_warnings, write_pid

BANNER = r"""
      __        __         __              ____        __             __
 _  _ / /__ ___ / /  ___ __/ /  ___  ___  / __ \___  / /  ___  ___  / /_
| |/ / / -_|_-</ _ \/ _ `/ _ \ / _ \/ _ \/ / / / _ \/ _ \/ _ \/ _ \/ __/
|___/_/\__/___/_.__/\_,_/_.__/\___/ \___/_/ /_/\___/_.__/\___/\___/\__/
"""


def print_help() -> None:
    print(BANNER)
    print(f"  {APP_NAME} v{__version__} —— 仿 NapCat 的微信 OneBot 11 网关\n")
    print("  用法： python3 -m wechat_onebot [选项]")
    print()
    print("  选项：")
    print("    --config-dir DIR   配置目录（默认 ./config）")
    print("    --driver NAME      覆盖微信驱动：mock | ipad | bridge")
    print("    --log-level LVL    DEBUG / INFO / WARN / ERROR（默认 INFO）")
    print("    --qr               终端扫码登录微信（仅 ipad 驱动）")
    print("    --no-open          配合 --qr，不自动打开二维码图片")
    print("    --stop             停止由本程序启动的实例（读取 run/gateway.pid）")
    print("    --no-preflight     跳过启动前的端口占用检查")
    print("    --check            只检查配置并退出")
    print("    --version          输出版本号")
    print("    --help             显示帮助")
    print()
    print("  文档：README.md")
    print()


async def run_qr(config_dir: Path, open_image: bool = True) -> int:
    """终端扫码登录：保存二维码 PNG 并自动打开，轮询直到成功。"""
    store = ConfigStore(config_dir)
    if store.driver_name != "ipad":
        print(f"当前驱动是 {store.driver_name}，只有 ipad 驱动需要扫码登录。")
        print("把 config/wechat.json 的 \"driver\" 改成 \"ipad\" 再试。")
        return 1

    ipad_cfg = store.wechat.get("ipad") or {}
    login_cfg = ipad_cfg.get("login") or {}
    http = IpadHttp(
        str(ipad_cfg.get("base_url") or "http://127.0.0.1:3010"),
        str(ipad_cfg.get("api_prefix") or "/api"),
        float(ipad_cfg.get("timeout") or 20),
        str(ipad_cfg.get("wxid") or ""),
    )
    client = IpadLoginClient(
        http,
        device_id=str(login_cfg.get("device_id") or ""),
        device_name=str(login_cfg.get("device_name") or "wechat-onebot"),
        login_type=str(login_cfg.get("login_type") or ""),
        login_mode=str(ipad_cfg.get("login_mode") or "ipad"),
    )

    print(f"协议服务：{http.base}")
    try:
        qr = await client.get_qr()
    except DriverError as exc:
        print(f"获取二维码失败：{exc}")
        print("请确认协议服务已启动（wechat.json -> ipad.base_url）。")
        return 1

    print(f"二维码 uuid：{qr.get('uuid')}")
    if qr.get("url"):
        print(f"二维码链接：{qr['url']}")
    png = qr_png_bytes(qr)
    if png:
        target = config_dir / "login-qr.png"
        target.write_bytes(png)
        print(f"二维码已保存：{target}")
        if open_image and sys.platform == "darwin":
            try:
                subprocess.Popen(["open", str(target)])
                print("已用「预览」打开二维码，请用手机微信扫码。")
            except Exception:
                pass
    else:
        print("协议服务没有返回二维码图片，请改用 WebUI 扫码或复制上面的链接。")
    print("等待扫码…（Ctrl+C 退出）")

    last_stage = None
    while True:
        await asyncio.sleep(2)
        try:
            result = await client.check_qr(str(qr.get("uuid") or ""))
        except DriverError as exc:
            message = str(exc)
            print(f"检查登录状态失败：{message}")
            if any(word in message for word in ("过期", "expire", "expired")):
                return 2
            continue
        stage = result.get("stage")
        if stage != last_stage:
            print(f"状态：{result.get('message') or stage}")
            last_stage = stage
        if stage == "success":
            account = result.get("account") or {}
            wxid = str(account.get("user_id") or "")
            nickname = str(account.get("nickname") or wxid)
            print(f"\n登录成功：{nickname}（{wxid}）")
            raw = store.raw("wechat")
            raw.setdefault("ipad", {})["wxid"] = wxid
            raw.setdefault("account", {}).update({"wxid": wxid, "nickname": nickname})
            store.update_raw("wechat", raw)
            store.load()
            print("已写入 config/wechat.json，下次启动会自动登录。")
            return 0
        if stage == "expired":
            print("二维码已过期，请重新运行 ./start.sh --qr")
            return 2


async def run(config_dir: Path, driver: str | None) -> None:
    store = ConfigStore(config_dir)
    if driver:
        store.wechat["driver"] = driver.strip().lower()
        print(f"  · 已用命令行覆盖驱动：{store.wechat['driver']}")

    pid_file = write_pid(config_dir)
    gateway = Gateway(store)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, gateway.request_stop)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass

    print(BANNER)
    print(f"  {APP_NAME} v{__version__}")
    print(f"  · 配置目录：{config_dir}")
    print(f"  · 微信驱动：{store.driver_name}")
    network = store.onebot11.get("network") or {}
    ws_servers = [cfg for cfg in (network.get("websocketServers") or []) if cfg.get("enable")]
    ws_clients = [cfg for cfg in (network.get("websocketClients") or []) if cfg.get("enable")]
    http_servers = [cfg for cfg in (network.get("httpServers") or []) if cfg.get("enable")]
    for cfg in ws_servers:
        host = "127.0.0.1" if str(cfg.get("host")) in ("0.0.0.0", "::") else cfg.get("host")
        print(f"  · 正向 WS：ws://{host}:{cfg.get('port')}{cfg.get('path') or '/onebot'}")
        print(f"    Koishi: adapter-napcat → protocol=ws → endpoint=ws://{host}:{cfg.get('port')}{cfg.get('path') or '/onebot'}")
    for cfg in ws_clients:
        print(f"  · 反向 WS：{cfg.get('url')}（由本程序连 Koishi 的 ws-reverse）")
    for cfg in http_servers:
        print(f"  · HTTP API：http://{cfg.get('host')}:{cfg.get('port')}")
    webui_cfg = store.webui_config()
    if webui_cfg.get("enable"):
        print(f"  · WebUI：http://127.0.0.1:{webui_cfg.get('port')}  （令牌见 config/webui.json）")
    print()

    try:
        await gateway.run_forever()
    finally:
        remove_pid(config_dir)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_simple_args(argv)
    if "help" in args or "h" in args:
        print_help()
        return 0
    if "version" in args:
        print(__version__)
        return 0

    set_level(args.get("log-level", "INFO"))
    config_dir = resolve_config_dir(args.get("config-dir"))

    if "qr" in args:
        try:
            return asyncio.run(run_qr(config_dir, open_image="no-open" not in args))
        except KeyboardInterrupt:
            return 130

    if "check" in args:
        store = ConfigStore(config_dir)
        print(f"配置目录：{store.dir}")
        print(f"驱动：{store.driver_name}")
        print(f"onebot11：{store.paths['onebot11']}")
        print(f"wechat：{store.paths['wechat']}")
        print(f"webui：{store.paths['webui']}")
        problems = preflight_check(store)
        if problems:
            print("端口检查：")
            for item in problems:
                print(f"  · {item}")
        else:
            print("端口检查：全部可用")
        for item in preflight_warnings(store):
            print(f"提示：{item}")
        print("配置格式检查通过")
        return 0

    store = ConfigStore(config_dir)
    if "stop" in args:
        return stop_instance(store)

    if "no-preflight" not in args:
        problems = preflight_check(store)
        if problems:
            print("启动前检查发现问题：")
            for item in problems:
                print(f"  · {item}")
            print()
            print("处理建议：")
            print("  ./start.sh --stop          停止由本程序启动的旧实例")
            print("  或修改 config/onebot11.json / config/wechat.json / config/webui.json 里的端口")
            return 2
        for item in preflight_warnings(store):
            print(f"提示：{item}")

    try:
        asyncio.run(run(config_dir, args.get("driver")))
    except StartupError as exc:
        print(f"启动失败：{exc}")
        print("请检查上面的日志，或运行 ./scripts/doctor.sh 做一次诊断。")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
