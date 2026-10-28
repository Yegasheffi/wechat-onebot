/* wechat-onebot WebUI —— 无框架，原生 JS。 */

(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const state = {
    status: null,
    configs: null,
    currentConfig: "onebot11",
    mock: null,
    currentChat: null,
    events: [],
    logs: [],
    autoscroll: true,
    login: { state: null, qr: null, pollTimer: null, tickTimer: null, autoTried: false },
    statusTimer: null,
    logSource: null,
    eventSource: null,
  };

  // ---------------------------------------------------------------- 工具

  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.remove("show"), 2200);
  }

  function formatDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d) return `${d} 天 ${h} 小时`;
    if (h) return `${h} 小时 ${m} 分`;
    if (m) return `${m} 分 ${seconds % 60} 秒`;
    return `${seconds} 秒`;
  }

  function formatTime(ts) {
    if (!ts) return "--:--:--";
    const date = new Date(ts);
    return date.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(() => toast("已复制"), () => toast("复制失败"));
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      toast("已复制");
    } catch (error) {
      toast("复制失败");
    }
    area.remove();
  }

  async function api(path, options = {}) {
    const init = { credentials: "same-origin", ...options };
    if (init.body && typeof init.body !== "string") {
      init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
      init.body = JSON.stringify(init.body);
    }
    const response = await fetch(path, init);
    if (response.status === 401) {
      showLogin();
      throw new Error("unauthorized");
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      const message = (payload && (payload.error || payload.message)) || `HTTP ${response.status}`;
      throw new Error(message);
    }
    return payload;
  }

  // ---------------------------------------------------------------- 登录

  function showLogin() {
    $("#login-mask").classList.add("show");
    setTimeout(() => $("#login-token").focus(), 60);
  }

  function hideLogin() {
    $("#login-mask").classList.remove("show");
    $("#login-error").textContent = "";
  }

  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = $("#login-token").value.trim();
    try {
      await api("/api/login", { method: "POST", body: { token } });
      hideLogin();
      $("#login-token").value = "";
      await refreshAll();
      connectStreams();
    } catch (error) {
      $("#login-error").textContent = error.message === "unauthorized" ? "令牌错误" : error.message;
    }
  });

  // ---------------------------------------------------------------- 导航

  const VIEW_META = {
    overview: { title: "概览", subtitle: "微信账号的 OneBot 11 网关" },
    login: { title: "微信登录", subtitle: "扫码登录，账号信息会自动保存" },
    simulator: { title: "消息模拟", subtitle: "不用真实微信，也能调通机器人插件" },
    config: { title: "网络配置", subtitle: "onebot11.json / wechat.json / webui.json" },
    logs: { title: "运行日志", subtitle: "实时日志与调试信息" },
  };

  function switchView(name) {
    if (!VIEW_META[name]) name = "overview";
    if (location.hash.slice(1) !== name) {
      history.replaceState(null, "", "#" + name);
    }
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
    $$(".view").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === name));
    const meta = VIEW_META[name] || VIEW_META.overview;
    $("#view-title").textContent = meta.title;
    $("#view-subtitle").textContent = meta.subtitle;
    if (name === "config" && !state.configs) loadConfig();
    if (name === "login") {
      refreshLoginState()
        .then(() => maybeAutoLoginQr())
        .catch(() => {});
    }
    if (name === "simulator") loadMockState();
    if (name === "logs") {
      requestAnimationFrame(() => {
        if (state.autoscroll) scrollLogsToBottom();
      });
    }
  }

  $("#nav").addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item");
    if (button) switchView(button.dataset.view);
  });

  $("#refresh-button").addEventListener("click", async () => {
    await refreshAll();
    toast("已刷新");
  });

  // ---------------------------------------------------------------- 状态

  async function refreshAll() {
    try {
      state.status = await api("/api/status");
      renderStatus();
      if (state.mock) await loadMockState(true);
    } catch (error) {
      if (error.message !== "unauthorized") {
        $("#side-state").textContent = "连接失败";
        $("#side-dot").className = "status-dot offline";
      }
    }
  }

  function renderStatus() {
    const status = state.status;
    if (!status) return;
    const account = status.account || {};
    const driver = status.driver_status || {};

    $("#side-state").textContent = status.online
      ? "已连接"
      : (status.logged_in === false ? "未登录" : "未连接");
    $("#side-dot").className = "status-dot " + (status.online ? "online" : "offline");
    $("#side-account").textContent = `${account.nickname || "—"} · ${status.driver || "—"}`;

    const wechatLogin = status.wechat_login || {};
    const needLogin = Boolean(wechatLogin.supported) && !wechatLogin.logged_in;
    $("#hero-name").textContent = needLogin ? "未登录" : (account.nickname || "—");
    $("#hero-id").textContent = needLogin ? "打开「微信登录」扫码" : (account.user_id || "—");
    $("#hero-avatar").textContent = needLogin ? "?" : (account.nickname || "微").slice(0, 1);
    $("#pill-driver").textContent = `驱动 ${status.driver || "—"}`;
    $("#pill-uptime").textContent = `运行 ${formatDuration(status.uptime)}`;
    $("#pill-version").textContent = `v${status.version || "—"}`;
    const login = status.wechat_login || {};
    const loginPill = $("#pill-login");
    if (loginPill) {
      loginPill.hidden = !login.supported;
      loginPill.textContent = login.logged_in ? "已登录" : "未登录";
      loginPill.classList.toggle("accent", Boolean(login.logged_in));
    }

    $("#stat-clients").textContent = String((status.clients || []).length);
    $("#stat-clients-hint").textContent = status.endpoints && status.endpoints.websocket && status.endpoints.websocket.length
      ? "WebSocket"
      : "未监听";
    $("#stat-recv").textContent = String((status.stats && status.stats.messages) || 0);
    $("#stat-sent").textContent = String((status.stats && status.stats.sent_messages) || 0);
    $("#stat-cache").textContent = String((status.store && status.store.cached) || 0);
    $("#stat-cache-hint").textContent = `容量 ${(status.store && status.store.capacity) || 0}`;

    renderEndpoints(status.endpoints || {});
    renderClients(status.clients || []);

    if (status.online === false) {
      $("#side-dot").className = "status-dot offline";
    }
  }

  function normalizeHost(url) {
    return url.replace("://0.0.0.0", "://127.0.0.1");
  }

  function renderEndpoints(endpoints) {
    const list = $("#endpoint-list");
    list.textContent = "";
    const rows = [];
    (endpoints.websocket || []).forEach((url) => rows.push(["WebSocket 服务端", normalizeHost(url)]));
    (endpoints.reverse || []).forEach((url) => rows.push(["反向 WS 目标", url]));
    (endpoints.http || []).forEach((url) => rows.push(["HTTP API", normalizeHost(url)]));
    (endpoints.media || []).forEach((url) => rows.push(["媒体地址", url]));

    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "endpoint empty";
      empty.textContent = "尚未启用任何网络端点";
      list.appendChild(empty);
      return;
    }
    rows.forEach(([label, value]) => {
      const row = document.createElement("div");
      row.className = "endpoint";
      const code = document.createElement("code");
      code.textContent = value;
      const left = document.createElement("span");
      left.className = "endpoint-label";
      left.textContent = label;
      const wrap = document.createElement("span");
      wrap.style.display = "flex";
      wrap.style.flexDirection = "column";
      wrap.style.gap = "2px";
      wrap.style.minWidth = "0";
      wrap.appendChild(left);
      wrap.appendChild(code);
      const button = document.createElement("button");
      button.className = "button mini";
      button.textContent = "复制";
      button.addEventListener("click", () => copyText(value));
      row.appendChild(wrap);
      row.appendChild(button);
      list.appendChild(row);
    });
  }

  function renderClients(clients) {
    const list = $("#client-list");
    list.textContent = "";
    $("#client-count-badge").textContent = String(clients.length);
    if (!clients.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "暂无客户端连接。把 OneBot 11 客户端指向上面的 WebSocket 地址即可。";
      list.appendChild(empty);
      return;
    }
    clients.forEach((client) => {
      const row = document.createElement("div");
      row.className = "client-row";
      const name = document.createElement("span");
      name.className = "client-name";
      name.textContent = client.name || "无名连接";
      const role = document.createElement("span");
      role.className = "client-role";
      role.textContent = `${client.role === "forward-ws" ? "正向 WS" : "反向 WS"} · ${client.post_format}${client.report_self_message ? " · 上报自身消息" : ""}`;
      row.appendChild(name);
      row.appendChild(role);
      list.appendChild(row);
    });
  }

  // ---------------------------------------------------------------- 模拟器

  async function loadMockState(quiet = false) {
    try {
      const mock = await api("/api/mock/state");
      state.mock = mock;
      renderMockList();
      if (!quiet && !mock.available) {
        const bubbles = $("#bubbles");
        bubbles.textContent = "";
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = `当前驱动是 ${mock.driver || "—"}，模拟器仅在 driver=mock 时可用。`;
        bubbles.appendChild(empty);
      }
    } catch (error) {
      if (error.message !== "unauthorized") toast(error.message);
    }
  }

  function renderMockList() {
    const list = $("#sim-chat-list");
    list.textContent = "";
    const mock = state.mock;
    if (!mock || !mock.available) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = "不可用";
      list.appendChild(empty);
      return;
    }
    const groups = mock.groups || [];
    const friends = mock.friends || [];
    groups.forEach((group) => {
      list.appendChild(buildChatButton({
        key: `group:${group.group_id}`,
        name: group.name,
        tag: `${(group.members || []).length} 人`,
        type: "group",
        chatType: "group",
        chatId: group.group_id,
        subtitle: group.group_id,
      }));
    });
    friends.forEach((friend) => {
      list.appendChild(buildChatButton({
        key: `private:${friend.user_id}`,
        name: friend.name,
        tag: "",
        type: "private",
        chatType: "private",
        chatId: friend.user_id,
        subtitle: friend.user_id,
      }));
    });
    if (!groups.length && !friends.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = "mock 里还没有会话";
      list.appendChild(empty);
    }
    if (!state.currentChat && groups.length) {
      selectChat({
        key: `group:${groups[0].group_id}`,
        name: groups[0].name,
        chatType: "group",
        chatId: groups[0].group_id,
        subtitle: groups[0].group_id,
      }, true);
    } else if (state.currentChat) {
      markActiveChat();
    }
  }

  function buildChatButton(info) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sim-chat";
    button.dataset.key = info.key;
    button.dataset.chatType = info.chatType;
    button.dataset.chatId = info.chatId;
    button.dataset.name = info.name;
    button.dataset.subtitle = info.subtitle || "";
    const avatar = document.createElement("span");
    avatar.className = "chat-avatar" + (info.type === "group" ? " group" : "");
    avatar.textContent = (info.name || "?").slice(0, 1);
    const name = document.createElement("span");
    name.className = "chat-name";
    name.textContent = info.name;
    const tag = document.createElement("span");
    tag.className = "chat-tag";
    tag.textContent = info.tag || "";
    button.appendChild(avatar);
    button.appendChild(name);
    if (info.tag) button.appendChild(tag);
    button.addEventListener("click", () => selectChat({
      key: info.key,
      name: info.name,
      chatType: info.chatType,
      chatId: info.chatId,
      subtitle: info.subtitle,
    }));
    return button;
  }

  function selectChat(info, silent = false) {
    state.currentChat = info;
    markActiveChat();
    $("#sim-title").textContent = info.name || info.chatId;
    $("#sim-subtitle").textContent = info.subtitle || "";
    $("#sim-input").disabled = false;
    $("#composer .send-button").disabled = false;
    renderBubbles();
    if (!silent) $("#sim-input").focus();
  }

  function markActiveChat() {
    $$("#sim-chat-list .sim-chat").forEach((item) => {
      item.classList.toggle("active", state.currentChat && item.dataset.key === state.currentChat.key);
    });
  }

  function chatMatch(event, chat) {
    if (!chat) return false;
    if (chat.chatType === "group") return event.group_id === chat.chatId;
    return !event.group_id && (event.user_id === chat.chatId || event.target_id === chat.chatId);
  }

  function renderBubbles() {
    const container = $("#bubbles");
    container.textContent = "";
    if (!state.currentChat) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = '<span class="empty-icon">💬</span><p>选一个会话，然后在这里冒充群友/好友发消息</p>';
      container.appendChild(empty);
      return;
    }
    const events = state.events.filter((event) => chatMatch(event, state.currentChat));
    if (!events.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = '<span class="empty-icon">✨</span><p>还没有消息，发一条试试</p>';
      container.appendChild(empty);
      return;
    }
    events.forEach((event) => container.appendChild(buildBubble(event)));
    container.scrollTop = container.scrollHeight;
  }

  function buildBubble(event) {
    const row = document.createElement("div");
    if (event.post_type === "message_sent") {
      row.className = "bubble-row self";
    } else if (event.post_type !== "message") {
      row.className = "bubble-row notice";
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = event.sub_type || event.post_type || "事件";
      row.appendChild(bubble);
      return row;
    } else {
      row.className = "bubble-row";
    }
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = event.text || summarizeSegments(event.segments);
    row.appendChild(bubble);
    const meta = document.createElement("span");
    meta.className = "bubble-meta";
    meta.textContent = event.post_type === "message_sent"
      ? "机器人"
      : (event.sender_name || event.user_id || "");
    if (event.post_type === "message_sent") {
      row.insertBefore(meta, bubble);
    } else {
      row.appendChild(meta);
    }
    return row;
  }

  function summarizeSegments(segments) {
    if (!Array.isArray(segments)) return "";
    return segments.map((segment) => {
      switch (segment.type) {
        case "text": return (segment.data && segment.data.text) || "";
        case "image": return "[图片]";
        case "record": return "[语音]";
        case "video": return "[视频]";
        case "file": return "[文件]";
        case "at": return "@" + ((segment.data && (segment.data.name || segment.data.qq)) || "");
        case "reply": return "";
        default: return `[${segment.type}]`;
      }
    }).join("");
  }

  $("#composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#sim-input");
    const text = input.value.trim();
    if (!text || !state.currentChat) return;
    const chat = state.currentChat;
    input.value = "";
    try {
      await api("/api/mock/message", {
        method: "POST",
        body: { chat_type: chat.chatType, chat_id: chat.chatId, text },
      });
    } catch (error) {
      if (error.message !== "unauthorized") toast(error.message);
      input.value = text;
      return;
    }
    input.focus();
  });


  // ---------------------------------------------------------------- 微信登录

  function loginElements() {
    return {
      badge: $("#login-badge"),
      dot: $("#login-dot"),
      status: $("#login-status"),
      countdown: $("#login-countdown"),
      image: $("#qr-image"),
      placeholder: $("#qr-placeholder"),
      refresh: $("#qr-refresh"),
      logout: $("#login-logout"),
      nickname: $("#login-nickname"),
      wxid: $("#login-wxid"),
      avatar: $("#login-avatar"),
      hint: $("#login-hint"),
      service: $("#login-service"),
      copy: $("#login-copy-wxid"),
    };
  }

  async function refreshLoginState() {
    const data = await api("/api/login/state");
    state.login.state = data.state || {};
    renderLogin(state.login.state);
    return state.login.state;
  }

  function renderLogin(snapshot) {
    const view = loginElements();
    if (!view.badge) return;
    const supported = Boolean(snapshot && snapshot.supported);
    const loggedIn = Boolean(snapshot && snapshot.logged_in);
    const serviceOk = !snapshot || snapshot.service_ok !== false;
    const account = (snapshot && snapshot.account) || {};
    const qr = state.login.qr || (snapshot && snapshot.qr) || null;

    view.badge.textContent = !supported ? "无需登录" : loggedIn ? "已登录" : "未登录";
    view.badge.className = "badge " + (loggedIn ? "success" : "subtle");
    const scanned = state.login.stage === "scanned";
    view.dot.className = "status-dot " + (loggedIn || !supported ? "online" : !serviceOk ? "offline" : "warn");
    view.status.textContent = !supported
      ? "该驱动无需登录"
      : loggedIn
        ? `已登录：${account.nickname || account.user_id || ""}`
        : !serviceOk
          ? "协议服务不可达"
          : (state.login.stageText || "未登录");

    view.nickname.textContent = loggedIn
      ? (account.nickname || account.user_id || "已登录")
      : (supported ? "未登录" : "无需登录");
    view.wxid.textContent = loggedIn
      ? (account.user_id || "—")
      : (supported ? "扫码后显示账号信息" : "mock / bridge 驱动直接可用");
    view.avatar.textContent = (loggedIn && (account.nickname || account.user_id) || "微").slice(0, 1);
    if (loggedIn && account.avatar) {
      view.avatar.style.backgroundImage = `url("${account.avatar}")`;
      view.avatar.textContent = "";
    } else {
      view.avatar.style.backgroundImage = "";
    }

    view.refresh.hidden = !supported || loggedIn;
    view.refresh.disabled = !supported || loggedIn;
    view.logout.disabled = !loggedIn;
    const protocol = snapshot && snapshot.protocol;
    const protocolLabel = protocol === "legacy" ? "v3.1 兼容" : protocol === "v3" ? "v3.2" : "";
    view.service.textContent = ((snapshot && snapshot.service) || "—") + (protocolLabel ? `（${protocolLabel}）` : "");
    if (view.copy) {
      view.copy.hidden = !loggedIn || !(account.user_id);
      view.copy.onclick = () => {
        if (account.user_id) copyText(account.user_id);
      };
    }
    view.hint.textContent = !supported
      ? "当前驱动不需要登录；切换到 ipad 驱动后即可扫码。"
      : loggedIn
        ? `客户端配置里的 selfId 请填成 ${account.user_id || "上面的 wxid"}，否则 self_id 会对不上。`
        : !serviceOk
          ? "协议服务不可达：检查 docker logs wechat-ipad-v311，或运行 ./scripts/doctor.sh。"
          : state.login.error || "登录成功后 wxid 会写入 config/wechat.json，下次启动自动登录。";
    view.hint.classList.toggle("error", Boolean(state.login.error) && !loggedIn);

    if (!supported) {
      view.image.hidden = true;
      view.placeholder.style.display = "grid";
      view.placeholder.innerHTML = "<span>当前驱动无需登录</span>";
      view.countdown.textContent = "";
      stopLoginPolling();
      return;
    }

    if (qr && !loggedIn) {
      view.image.src = qr.image || "";
      view.image.hidden = !qr.image;
      view.placeholder.style.display = qr.image ? "none" : "grid";
      if (!qr.image) {
        view.placeholder.innerHTML = `<span>没有拿到二维码图片<br />可复制链接在浏览器打开：<br /><code>${qr.url || "—"}</code></span>`;
      }
      startLoginTicker();
    } else {
      view.image.hidden = true;
      view.image.removeAttribute("src");
      view.placeholder.style.display = "grid";
      view.placeholder.innerHTML = loggedIn
        ? "<span>已登录 ✅</span>"
        : "<span>点击「获取二维码」<br />用手机微信扫码登录</span>";
      view.countdown.textContent = "";
    }
  }

  async function maybeAutoLoginQr() {
    const snapshot = state.login.state;
    if (!snapshot || !snapshot.supported || snapshot.logged_in) return;
    if (state.login.qr || state.login.autoTried) return;
    state.login.autoTried = true;
    try {
      await createLoginQr(false);
    } catch (error) {
      if (error.message !== "unauthorized") {
        state.login.stageText = "获取二维码失败";
        toast("获取二维码失败：" + error.message);
        renderLogin(snapshot);
      }
    }
  }

  async function createLoginQr(showToast = true) {
    stopLoginPolling();
    state.login.error = "";
    state.login.stageText = "正在获取二维码…";
    renderLogin(state.login.state);
    const data = await api("/api/login/qr", { method: "POST" });
    state.login.qr = data.qr;
    state.login.state = data.state || {};
    state.login.autoTried = true;
    state.login.stageText = "等待扫码";
    state.login.stage = "waiting";
    renderLogin(state.login.state);
    startLoginPolling();
    if (showToast) toast("二维码已生成，请用微信扫码");
    return data.qr;
  }

  function startLoginPolling() {
    stopLoginPolling();
    state.login.pollTimer = setInterval(checkLoginQr, 2000);
  }

  function stopLoginPolling() {
    if (state.login.pollTimer) {
      clearInterval(state.login.pollTimer);
      state.login.pollTimer = null;
    }
  }

  function startLoginTicker() {
    if (state.login.tickTimer) return;
    state.login.tickTimer = setInterval(() => {
      const view = loginElements();
      const qr = state.login.qr;
      if (!qr || !qr.expires_at) {
        view.countdown.textContent = "";
        return;
      }
      const remain = Math.max(0, Math.floor(qr.expires_at - Date.now() / 1000));
      if (remain > 0) {
        view.countdown.textContent = `${String(Math.floor(remain / 60)).padStart(2, "0")}:${String(remain % 60).padStart(2, "0")}`;
      } else {
        view.countdown.textContent = "已过期";
        stopLoginPolling();
      }
    }, 1000);
  }

  async function checkLoginQr() {
    const qr = state.login.qr;
    if (!qr || !qr.uuid) {
      stopLoginPolling();
      return;
    }
    try {
      const data = await api("/api/login/check?uuid=" + encodeURIComponent(qr.uuid));
      const result = data.result || {};
      if (result.expires_at && state.login.qr) {
        state.login.qr.expires_at = result.expires_at;
      }
      state.login.state = data.state || {};
      if (result.stage === "success") {
        state.login.qr = null;
        state.login.stageText = "";
        state.login.error = "";
        stopLoginPolling();
        renderLogin(state.login.state);
        toast("微信登录成功");
        await refreshAll();
        await refreshLoginState();
        return;
      }
      state.login.stage = result.stage;
      if (result.stage === "expired") {
        state.login.qr = null;
        state.login.stageText = "二维码已过期";
        stopLoginPolling();
        renderLogin(data.state || {});
        return;
      }
      state.login.stageText = result.stage === "scanned" ? "已扫码，请在手机上确认" : "等待扫码";
      state.login.state = data.state || state.login.state;
      renderLogin(state.login.state);
    } catch (error) {
      if (error.message === "unauthorized") return;
      stopLoginPolling();
      state.login.stageText = "登录失败";
      state.login.error = error.message;
      renderLogin(state.login.state);
      toast(error.message);
    }
  }

  async function logoutWechat() {
    try {
      const data = await api("/api/login/logout", { method: "POST" });
      state.login.qr = null;
      state.login.autoTried = false;
      state.login.state = data.state || {};
      stopLoginPolling();
      renderLogin(state.login.state);
      toast("已退出微信登录");
      await refreshAll();
    } catch (error) {
      if (error.message !== "unauthorized") toast(error.message);
    }
  }

  $("#qr-refresh").addEventListener("click", () => {
    state.login.autoTried = true;
    createLoginQr(true).catch((error) => {
      if (error.message === "unauthorized") return;
      state.login.stageText = "获取二维码失败";
      renderLogin(state.login.state);
      toast(error.message);
    });
  });

  $("#login-logout").addEventListener("click", () => {
    logoutWechat();
  });

  // ---------------------------------------------------------------- 配置

  async function loadConfig() {
    try {
      state.configs = await api("/api/config");
      renderConfig();
    } catch (error) {
      if (error.message !== "unauthorized") toast(error.message);
    }
  }

  function renderConfig() {
    if (!state.configs) return;
    const name = state.currentConfig;
    const data = state.configs[name] || {};
    $("#config-editor").value = JSON.stringify(data, null, 2);
    const path = (state.configs.paths && state.configs.paths[name]) || `config/${name}.json`;
    $("#config-path").textContent = path;
    $("#config-hint").textContent = name === "onebot11"
      ? "改动监听端口 / token / WS 模式后需要重启进程才生效。"
      : name === "wechat"
        ? "切换驱动或修改回调端口后需要重启进程。driver=mock 时可在「消息模拟」里造消息。"
        : "修改 WebUI 端口或令牌后需要重启进程，并重新登录。";
  }

  $("#config-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-config]");
    if (!button) return;
    state.currentConfig = button.dataset.config;
    $$("#config-tabs button").forEach((item) => item.classList.toggle("active", item === button));
    renderConfig();
  });

  $("#config-reload").addEventListener("click", () => {
    renderConfig();
    toast("已恢复到上次保存的内容");
  });

  $("#config-save").addEventListener("click", async () => {
    const name = state.currentConfig;
    const raw = $("#config-editor").value;
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      toast("JSON 格式错误：" + error.message);
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      toast("配置必须是 JSON 对象");
      return;
    }
    try {
      const result = await api("/api/config", { method: "POST", body: { name, data: parsed } });
      toast(result.restart_required ? "已保存，重启进程后生效" : "已保存");
      await loadConfig();
    } catch (error) {
      if (error.message !== "unauthorized") toast(error.message);
    }
  });

  // ---------------------------------------------------------------- 日志

  function appendLog(item) {
    state.logs.push(item);
    if (state.logs.length > 600) state.logs.shift();
    const view = $("#log-view");
    view.appendChild(buildLogLine(item));
    while (view.childElementCount > 600) view.removeChild(view.firstElementChild);
    if (state.autoscroll) scrollLogsToBottom();
  }

  function buildLogLine(item) {
    const line = document.createElement("div");
    line.className = "log-line";
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatTime(item.time || Date.now());
    const level = document.createElement("span");
    level.className = "log-level " + (item.level || "INFO");
    level.textContent = item.level || "INFO";
    const name = document.createElement("span");
    name.className = "log-name";
    name.textContent = String(item.name || "").replace("wechat_onebot.", "");
    const message = document.createElement("span");
    message.className = "log-message";
    message.textContent = item.message || "";
    line.appendChild(time);
    line.appendChild(level);
    line.appendChild(name);
    line.appendChild(message);
    return line;
  }

  function scrollLogsToBottom() {
    const view = $("#log-view");
    view.scrollTop = view.scrollHeight;
  }

  $("#autoscroll").addEventListener("change", (event) => {
    state.autoscroll = event.target.checked;
    if (state.autoscroll) scrollLogsToBottom();
  });

  async function loadLogs() {
    try {
      const result = await api("/api/logs?limit=200");
      const view = $("#log-view");
      view.textContent = "";
      state.logs = [];
      (result.logs || []).forEach((item) => {
        state.logs.push(item);
        view.appendChild(buildLogLine(item));
      });
      scrollLogsToBottom();
    } catch (error) {
      if (error.message !== "unauthorized") toast(error.message);
    }
  }

  // ---------------------------------------------------------------- 事件 / SSE

  function onRealtimeEvent(event) {
    state.events.push(event);
    if (state.events.length > 400) state.events.shift();
    if (state.currentChat && chatMatch(event, state.currentChat)) {
      const container = $("#bubbles");
      const empty = container.querySelector(".empty-state");
      if (empty) empty.remove();
      container.appendChild(buildBubble(event));
      container.scrollTop = container.scrollHeight;
    }
    if (state.status) {
      if (event.post_type === "message") {
        state.status.stats.messages = (state.status.stats.messages || 0) + 1;
        $("#stat-recv").textContent = String(state.status.stats.messages);
      } else if (event.post_type === "message_sent") {
        state.status.stats.sent_messages = (state.status.stats.sent_messages || 0) + 1;
        $("#stat-sent").textContent = String(state.status.stats.sent_messages);
      }
    }
  }

  function connectStreams() {
    if (state.logSource) state.logSource.close();
    if (state.eventSource) state.eventSource.close();

    state.logSource = new EventSource("/api/logs/stream");
    state.logSource.addEventListener("log", (message) => {
      try {
        appendLog(JSON.parse(message.data));
      } catch (error) { /* 忽略 */ }
    });

    state.eventSource = new EventSource("/api/events/stream");
    state.eventSource.addEventListener("event", (message) => {
      try {
        onRealtimeEvent(JSON.parse(message.data));
      } catch (error) { /* 忽略 */ }
    });
  }

  async function loadRecentEvents() {
    try {
      const result = await api("/api/events/recent");
      state.events = result.events || [];
      if (state.currentChat) renderBubbles();
    } catch (error) {
      if (error.message !== "unauthorized") toast(error.message);
    }
  }

  // ---------------------------------------------------------------- 启动

  async function boot() {
    try {
      state.status = await api("/api/status");
    } catch (error) {
      if (error.message === "unauthorized") return;
    }
    renderStatus();
    const initial = location.hash.slice(1);
    if (initial && VIEW_META[initial]) switchView(initial);
    await Promise.all([loadLogs(), loadRecentEvents()]);
    refreshLoginState().catch(() => {});
    connectStreams();
    state.statusTimer = setInterval(refreshAll, 5000);
  }

  boot();
})();
