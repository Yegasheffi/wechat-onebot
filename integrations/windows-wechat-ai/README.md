# Windows 微信 → wechat-onebot 桥接代理

用**真实的 Windows 微信 4.x** 驱动个人微信号，再通过 wechat-onebot 的 `bridge` 驱动接入 OneBot 11。

```
Windows 10/11
┌──────────────────────┐        HTTP          ┌──────────────────────┐
│ 微信 4.1.15.x（登录） │ ←── UIA/数据库 ──→   │ bridge_agent.py      │
└──────────────────────┘                      │  · 收消息 → /wechat/event
                                              │  · 拉任务 ← /wechat/actions
                                              │  · 发消息 → 微信
                                              └──────────┬───────────┘
                                                         │ HTTP 9002
                                              ┌──────────▼───────────┐
                                              │ wechat-onebot        │
                                              │ driver = bridge      │
                                              │  → OneBot WS :3001   │
                                              └──────────────────────┘
```

## 全部跑在 Windows 上

`wechat-onebot` 本身是纯 Python、零第三方依赖，可以直接在 Windows 上运行。推荐结构：

```
同一台 Windows
├── wechat-onebot/        # 网关（Python 3.10+）
└── bridge-agent/         # 本代理（Python 3.12 + wechatauto-replica）
```

- 网关监听 `127.0.0.1:9002`（bridge 回调）和 `127.0.0.1:3001`（OneBot WS）
- 代理把 `gateway` 填 `http://127.0.0.1:9002`
- Koishi / 其它 OneBot 客户端如果也在 Windows，连 `ws://127.0.0.1:3001/onebot`
- 全程不需要 Mac、不需要 Docker、不需要协议服务


## 快速使用

1. **网关**：`config/wechat.json` 把 `driver` 改成 `bridge`，回调端口 `9002` 对 Windows 可达，`bridge.token` 设一个值
2. **Windows**： Windows 10/11 + 微信 4.1.12+（已登录、窗口不要最小化）+ Python 3.12 64 位
3. 安装依赖并复制配置：

   ```bat
   python -m pip install -r requirements.txt
   copy config.example.json config.json
   ```

4. 编辑 `config.json`：`gateway` 默认填 `http://127.0.0.1:9002`（网关同机）；如果网关在别的机器，再改成它的局域网 IP
5. 先 `doctor.bat` 自检，再 `start.bat` 启动

更细的步骤、验证方法和排障见下方。

---

不走 WeChatFerry、不做 DLL 注入，也不依赖旧协议；走微信官方 4.x 客户端 + UIA 自动化，所以不会遇到
“你的版本过低，请升级”的协议登录拦截。

---

## 1. 网关侧（推荐：同一台 Windows）

把 `config/wechat.json` 的驱动切成 bridge。默认网关和代理都在同一台 Windows 上，回调走本机 9002：

```json
{
  "driver": "bridge",
  "callback": { "enable": true, "host": "0.0.0.0", "port": 9002 },
  "bridge": {
    "account": { "wxid": "", "nickname": "微信 AI" },
    "token": "change-me",
    "action_timeout": 35
  }
}
```

重启网关：

```bash
cd ~/wechat-onebot
./start.sh --stop
./start.sh
```

> 桥接路由挂在**回调端口 9002** 上（`/wechat/event`、`/wechat/actions`）。
> 网关和代理可以都跑在同一台 Windows 上；如果网关在另一台机器，再把 `gateway` 填成那台机器的局域网 IP。

---

## 2. Windows 侧

要求：

- **真实 Windows 10/11**（不是 CrossOver / Wine；UIA 和微信内存读取必须在真 Windows 上）
- **微信 4.1.12+**，已在 **4.1.15.53** 验证
- **Python 3.12 64 位**（python.org 安装包，勾选 Add to PATH）

步骤：

```bat
cd windows-wechat-ai
python -m pip install -r requirements.txt
copy config.example.json config.json
```

编辑 `config.json`：

```json
{
  "gateway": "http://127.0.0.1:9002",
  "token": "change-me",
  "poll_wait": 25,
  "request_timeout": 15,
  "account_nickname": "微信 AI",
  "log_file": "agent.log"
}
```

- `gateway`：默认 `http://127.0.0.1:9002`（网关同机）；网关在另一台机器时填它的局域网 IP
- `token`：与网关 `bridge.token` 一致
- 微信先登录，主窗口不要最小化；建议终端**以管理员身份运行**（UIA 需要和微信同权限）

先自检：

```bat
doctor.bat
```

看到微信账号、网关都通过后启动：

```bat
start.bat
```

---

## 3. 验证

1. 微信里给文件传输助手/群里发一条消息
2. 看 Windows 终端是否出现 `已发送 -> ...` / 消息上报日志
3. 在 Koishi 或其它 OneBot 客户端里应收到消息；网关日志会显示 `bridge` 驱动

Windows 端也可以先跑不依赖微信的自检（在任意 Python 3.9+ 上）：

```bash
python3 test_agent.py
# PASS：Windows 代理 → bridge 事件 / 拉取任务 / 微信发送 / 回传结果
```

本仓库开发时另外验证过：OneBot `send_group_msg` → 网关 bridge 队列 → 代理 → `win-...` 回执，
以及代理入站消息 → 网关 → OneBot `message` 事件，两条链路都通。

---

## 4. 行为与限制

- 只处理**文本**；图片/语音/文件会以 `[图片]`/`[语音]` 占位或跳过
- 群聊与私聊都支持；群里 @ 的判断由 Koishi 插件/网关侧处理，代理只负责原样转发文本
- 一个代理对应一个已登录微信账号；不要同时开多个代理
- 微信窗口需要保持可见；最小化或锁屏可能导致 UIA 发送失败
- 微信大版本更新可能改变控件树；`wechatauto-replica` 已适配 4.1.12–4.1.15，升级微信后先跑 `doctor.bat`
- 代理只监听本机已登录微信，不保存聊天记录到云端；`agent.log` 可能含消息摘要，注意保管

## 5. 常见问题

**`doctor.bat` 报无法连接微信**
- 微信要已登录，主窗口不能最小化
- 用管理员权限运行终端
- 确认 Python 是 64 位：`python -c "import struct;print(struct.calcsize('P')*8)"` 应为 64

**网关不可达**
- 浏览器访问 `http://127.0.0.1:9002/wechat/state`（网关同机）
- Windows 防火墙放行 9002；网关和代理同机时就用 `127.0.0.1`

**发送失败/打开错会话**
- 微信昵称重复时先把 `config.json` 里的目标改成更精确的备注名
- 4.1.15 的 UIA 布局已被 wechatauto-replica 适配；更老的 4.1.12 以下不支持

**Koishi 里 selfId 不对**
- 代理启动时会发 `type=account` 事件，网关 bridge 会自动把 self_id 更新为真实 wxid
- 仍建议在 Koishi 的适配器配置里把 selfId 改成该 wxid

---


