/* ==========================================================================
   微光 · 主动关怀 —— 配置面板前端

   运行在 AstrBot 的受限 iframe 里，所有后端调用必须经 window.AstrBotPluginPage
   bridge。全程原生 JS，不引入任何外部依赖（CSP 与离线场景都能用）。
   ========================================================================== */

const bridge = window.AstrBotPluginPage;

/* --------------------------------------------------------------------------
   工具
   -------------------------------------------------------------------------- */
const $ = (sel, root = document) => root.querySelector(sel);

function esc(input) {
  return String(input ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtClock(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtDelta(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} 天 ${h % 24} 小时` : `${d} 天`;
}

function toast(message, kind = "info", ttl = 3600) {
  const box = $("#toasts");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  box.appendChild(node);
  setTimeout(() => {
    node.classList.add("out");
    setTimeout(() => node.remove(), 300);
  }, ttl);
}

/* --------------------------------------------------------------------------
   API 封装
   -------------------------------------------------------------------------- */
async function api(endpoint, params) {
  return bridge.apiGet(endpoint, params || {});
}

async function post(endpoint, body) {
  return bridge.apiPost(endpoint, body || {});
}

/**
 * 页面内自绘确认框，返回 Promise<boolean>。
 *
 * 面板跑在 AstrBot 的受限 iframe 里。宿主若未授予 `allow-modals`，
 * `window.confirm()` 会被直接拦掉并返回 false，且不报任何错 ——
 * 表现就是「删除按钮点了没反应」。自绘框不依赖该权限。
 *
 * Esc 或点遮罩取消；Enter 确认。
 */
function askConfirm({ title, message, confirmText = "确定", cancelText = "取消", danger = true } = {}) {
  return new Promise((resolve) => {
    let done = false;
    let overlay = null;
    const finish = (value) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      if (overlay && overlay.remove) overlay.remove();
      resolve(value);
    };
    const onKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
      else if (event.key === "Enter") { event.preventDefault(); finish(true); }
    };

    overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true">
      <div class="modal-title">${esc(title || "确认")}</div>
      <div class="modal-msg">${esc(message || "")}</div>
      <div class="modal-actions">
        <button class="btn" data-cancel="1">${esc(cancelText)}</button>
        <button class="btn ${danger ? "danger" : "primary"}" data-ok="1">${esc(confirmText)}</button>
      </div>
    </div>`;
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) finish(false);
      else if (event.target.closest("[data-cancel]")) finish(false);
      else if (event.target.closest("[data-ok]")) finish(true);
    });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(overlay);
    const okBtn = overlay.querySelector("[data-ok]");
    if (okBtn) okBtn.focus();
  });
}

async function guard(fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) toast(okMessage, "ok");
    return result;
  } catch (error) {
    toast(error?.message || "操作失败", "err", 5200);
    return null;
  }
}

/* --------------------------------------------------------------------------
   后台动效：粒子网络
   -------------------------------------------------------------------------- */
function initNet() {
  const canvas = $("#net");
  if (!canvas || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const ctx = canvas.getContext("2d");
  let nodes = [];
  let width = 0;
  let height = 0;
  let pointer = { x: -999, y: -999 };
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  function resize() {
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const target = Math.min(78, Math.round((width * height) / 20000));
    nodes = Array.from({ length: target }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.28,
      vy: (Math.random() - 0.5) * 0.28,
      r: Math.random() * 1.5 + 0.7,
    }));
  }

  function frame() {
    ctx.clearRect(0, 0, width, height);

    for (const node of nodes) {
      node.x += node.vx;
      node.y += node.vy;
      if (node.x < 0 || node.x > width) node.vx *= -1;
      if (node.y < 0 || node.y > height) node.vy *= -1;

      const dx = node.x - pointer.x;
      const dy = node.y - pointer.y;
      const dist = Math.hypot(dx, dy);
      if (dist < 130 && dist > 0.1) {
        node.x += (dx / dist) * 0.35;
        node.y += (dy / dist) * 0.35;
      }
    }

    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i];
        const b = nodes[j];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d > 126) continue;
        ctx.strokeStyle = `rgba(255, 179, 92, ${(1 - d / 126) * 0.3})`;
        ctx.lineWidth = 0.6;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }

    ctx.fillStyle = "rgba(90, 209, 255, 0.62)";
    for (const node of nodes) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
      ctx.fill();
    }

    requestAnimationFrame(frame);
  }

  canvas.addEventListener("pointermove", (event) => {
    const rect = canvas.getBoundingClientRect();
    pointer = { x: event.clientX - rect.left, y: event.clientY - rect.top };
  });
  canvas.addEventListener("pointerleave", () => {
    pointer = { x: -999, y: -999 };
  });

  window.addEventListener("resize", resize);
  resize();
  requestAnimationFrame(frame);
}

/* --------------------------------------------------------------------------
   全局状态
   -------------------------------------------------------------------------- */
const state = {
  overview: {},
  config: {},
  warnings: [],
  providers: [],
  scheduler: {},
  checks: [],
  limits: {},
  sessions: [],
  memories: [],
  history: [],
  schedules: [],
  tab: "overview",
  memoryQuery: "",
  filterUmo: "",
  busy: false,
};

const TRIGGER_LABEL = {
  idle: "空闲唤醒",
  followup: "话题追问",
  random: "随机关怀",
  schedule: "定时问候",
  manual: "手动触发",
  preview: "预览",
};

const TABS = [
  ["overview", "概览"],
  ["sessions", "会话"],
  ["memory", "记忆"],
  ["schedule", "定时"],
  ["history", "日志"],
  ["settings", "设置"],
];

/* --------------------------------------------------------------------------
   数据加载
   -------------------------------------------------------------------------- */
async function loadBootstrap() {
  const data = await api("bootstrap");
  state.overview = data.overview || {};
  state.config = data.config || {};
  state.warnings = data.warnings || [];
  state.providers = data.providers || [];
  state.scheduler = data.scheduler || {};
  state.limits = data.limits || {};
  renderAll();
}

async function loadTab(force = false) {
  const tab = state.tab;
  if (tab === "sessions" || tab === "overview") {
    const data = await api("sessions");
    state.sessions = data.sessions || [];
  }
  if (tab === "memory") {
    const data = await api("memories", { umo: state.filterUmo, q: state.memoryQuery, limit: 300 });
    state.memories = data.memories || [];
  }
  if (tab === "history") {
    const data = await api("history", { umo: state.filterUmo, limit: 120 });
    state.history = data.history || [];
  }
  if (tab === "schedule") {
    const data = await api("schedules", { umo: state.filterUmo });
    state.schedules = data.schedules || [];
  }
  if (tab === "overview") {
    await refreshOverview();
    const data = await api("diagnostics");
    state.checks = data.checks || [];
    state.scheduler = data.scheduler || state.scheduler;
  }
  if (force) renderAll();
}

/* 概览横幅、指标条依赖 overview 数据；配置变更后必须重拉，否则会一直显示旧状态 */
async function refreshOverview() {
  try {
    const data = await api("overview");
    if (data && typeof data === "object") {
      state.overview = data;
      if (data.scheduler) state.scheduler = data.scheduler;
    }
  } catch (error) {
    /* 刷新失败不打断当前操作，下次进入概览页还会再试 */
  }
}

/* --------------------------------------------------------------------------
   渲染：指标条 / 标签页
   -------------------------------------------------------------------------- */
function renderStats() {
  const o = state.overview;
  const cards = [
    ["生效会话", o.active ?? "-", `共跟踪 ${o.sessions ?? 0} 个`],
    ["今日已发", o.sent_today ?? "-", `累计 ${o.sent_total ?? 0} 条`],
    ["被回应率", o.sent_total ? `${Math.round((o.answer_rate || 0) * 100)}%` : "-", `${o.answered ?? 0} 条有人接话`],
    ["记忆库", o.memories ?? "-", "长期记住的事"],
    ["暂停中", o.paused ?? "-", o.paused ? "连续没回应已自动闭嘴" : "暂无"],
  ];
  $("#stats").innerHTML = cards
    .map(
      ([k, v, s]) => `<div class="stat">
        <div class="k">${esc(k)}</div>
        <div class="v">${esc(v)}</div>
        <div class="s">${esc(s)}</div>
      </div>`
    )
    .join("");
}

function renderTabs() {
  $("#tabs").innerHTML = TABS.map(
    ([key, label]) =>
      `<button class="tab ${state.tab === key ? "active" : ""}" data-act="tab" data-tab="${key}" role="tab">${esc(label)}</button>`
  ).join("");
}

function renderTopbar() {
  const enabled = Boolean(state.config?.basic?.enabled);
  $("#master-switch").checked = enabled;
  $("#master-label").textContent = enabled ? "已开启" : "已关闭";

  const pill = $("#pill-scheduler");
  pill.classList.toggle("on", Boolean(state.scheduler?.running));
  pill.querySelector("span").textContent = state.scheduler?.running
    ? `调度器 ${state.scheduler.tick_count || 0} 次轮询`
    : "调度器未运行";

  $("#pill-dryrun").classList.toggle("hidden", !state.config?.advanced?.dry_run);
  $("#foot-time").textContent = `最后刷新 ${fmtTime(Date.now() / 1000)}`;
}

function renderAll() {
  renderTopbar();
  renderStats();
  renderTabs();
  renderView();
  startCountdown();
}

/* --------------------------------------------------------------------------
   渲染：视图分发
   -------------------------------------------------------------------------- */
function renderView() {
  const view = $("#view");
  const map = {
    overview: viewOverview,
    sessions: viewSessions,
    memory: viewMemory,
    schedule: viewSchedule,
    history: viewHistory,
    settings: viewSettings,
  };
  view.innerHTML = (map[state.tab] || viewOverview)();
}

function warningBanner() {
  if (!state.warnings.length) return "";
  return `<div class="banner warn">
    <strong>配置提醒</strong>
    <div>${state.warnings.map(esc).join("<br>")}</div>
  </div>`;
}

/* ---- 概览 ---- */
function viewOverview() {
  const o = state.overview;
  const sched = state.scheduler || {};
  const triggers = [
    ["空闲唤醒", state.config?.trigger?.idle_enable, state.config?.trigger?.idle_minutes, "分钟"],
    ["话题追问", state.config?.trigger?.followup_enable, state.config?.trigger?.followup_minutes, "分钟"],
    ["随机关怀", state.config?.trigger?.random_enable, `${state.config?.trigger?.random_min_per_day}~${state.config?.trigger?.random_max_per_day}`, "次/天"],
    ["定时问候", state.config?.trigger?.schedule_enable, state.schedules.length, "条规则"],
    [
      "即时搭话",
      state.config?.trigger?.instant_enable,
      `${state.config?.trigger?.instant_probability ?? 0}%`,
      "概率回复",
    ],
  ];

  const upcoming = state.sessions
    .filter((item) => item.eta)
    .sort((a, b) => a.eta - b.eta)
    .slice(0, 4);

  return `
    ${warningBanner()}
    ${
      o.enabled
        ? `<div class="banner ok"><strong>已开启</strong><div>微光会在满足条件时主动开口。想先看效果不想真的发出去，可以在「设置」里打开演练模式。</div></div>`
        : `<div class="banner"><strong>尚未开启</strong><div>总开关关闭中。打开开关，并确认「生效群号列表」里已经填了群号，微光才会说话。</div></div>`
    }

    <div class="grid bento">
      <div class="card span-2">
        <h2>调度器 <span class="hint">每 ${esc(sched.tick_seconds ?? 30)} 秒走一遍判定</span></h2>
        <div class="kv"><span class="k">状态</span><span class="v">${sched.running ? "运行中" : "未运行"}</span></div>
        <div class="kv"><span class="k">已轮询</span><span class="v">${esc(sched.tick_count ?? 0)} 次</span></div>
        <div class="kv"><span class="k">异常次数</span><span class="v">${esc(sched.error_count ?? 0)}</span></div>
        <div class="kv"><span class="k">上次轮询</span><span class="v">${esc(fmtTime(sched.last_tick_ts))}</span></div>
        <div class="kv"><span class="k">演练模式</span><span class="v">${sched.dry_run ? "开启（只生成不发送）" : "关闭"}</span></div>
        <div class="inline" style="margin-top:12px">
          <button class="btn" data-act="tick">立刻跑一次判定</button>
        </div>
        <p class="section-note" style="margin-top:10px">
          轮询本身不消耗模型额度，只有真的要生成时才会调用模型。
        </p>
      </div>

      <div class="card">
        <h2>触发方式</h2>
        ${triggers
          .map(
            ([name, on, value, unit]) => `
          <div class="kv">
            <span class="k">${esc(name)}</span>
            <span class="v">${on ? esc(value) + " " + esc(unit) : '<span style="opacity:.5">已关闭</span>'}</span>
          </div>`
          )
          .join("")}
      </div>

      <div class="card">
        <h2>防骚扰闸门</h2>
        <div class="kv"><span class="k">静默时段</span><span class="v">${esc(
          (state.limits.quiet?.start || []).join(":") + " → " + (state.limits.quiet?.end || []).join(":")
        )}</span></div>
        <div class="kv"><span class="k">每日上限</span><span class="v">${esc(state.limits.max_per_day)} 条</span></div>
        <div class="kv"><span class="k">两次间隔</span><span class="v">${esc(state.limits.cooldown_minutes)} 分钟</span></div>
        <div class="kv"><span class="k">距真人发言</span><span class="v">${esc(state.limits.min_human_gap_minutes)} 分钟</span></div>
        <div class="kv"><span class="k">无回应熔断</span><span class="v">连续 ${esc(state.limits.unanswered_pause)} 次</span></div>
      </div>

      <div class="card span-2">
        <h2>接下来的动作 <span class="hint">按预估时间排序</span></h2>
        ${
          upcoming.length
            ? `<div class="rows">${upcoming
                .map(
                  (item) => `<div class="row">
              <div class="row-main">
                <div class="row-title mono">${esc(item.name || item.target_id || item.umo)}</div>
                <div class="row-meta">
                  <span>${esc(item.eta_why)}</span>
                  <span class="countdown" data-eta="${item.eta}">${esc(fmtTime(item.eta))}</span>
                </div>
              </div>
            </div>`
                )
                .join("")}</div>`
            : `<div class="empty">当前没有排上队的动作。可能是总开关没开、不在生效范围、还在静默时段，或者所有条件都没满足。</div>`
        }
      </div>

      <div class="card span-2">
        <h2>自检</h2>
        ${state.checks
          .map(
            (item) => `<div class="check ${item.ok ? "pass" : "fail"}">
          <div class="mark">${item.ok ? "✓" : "!"}</div>
          <div>
            <div class="name">${esc(item.name)}</div>
            <div class="detail">${esc(item.detail)}</div>
          </div>
        </div>`
          )
          .join("") || '<div class="empty">暂无自检结果。</div>'}
      </div>

      <div class="card span-2">
        <h2>可用模型</h2>
        ${
          state.providers.length
            ? state.providers
                .map(
                  (item) => `<div class="kv"><span class="k mono">${esc(item.id)}</span><span class="v">${esc(item.label)}</span></div>`
                )
                .join("")
            : `<div class="empty">没有检测到任何对话模型。请先到 AstrBot 的「服务提供商」页添加一个，否则微光无法生成内容。</div>`
        }
      </div>
    </div>`;
}

/* ---- 会话 ---- */
function viewSessions() {
  if (!state.sessions.length) {
    return `<div class="empty">
      还没有跟踪到任何会话。<br>
      先让群友在目标群里说一句话，或者把「生效群号列表」填好之后等消息进来。
    </div>`;
  }

  return `<div class="sessions">
    ${state.sessions
      .map((item) => {
        const statusTag = item.paused
          ? `<span class="tag bad">已暂停</span>`
          : item.enabled
            ? `<span class="tag ok">生效中</span>`
            : `<span class="tag">已关闭</span>`;
        const scopeTag = `<span class="tag">${item.scope === "group" ? "群聊" : "私聊"}</span>`;
        const streak = item.unanswered_streak
          ? `<span class="tag ${item.unanswered_streak >= (state.limits.unanswered_pause || 3) - 1 ? "bad" : ""}">连续 ${item.unanswered_streak} 次无回应</span>`
          : "";

        return `<div class="session ${item.enabled ? "" : "off"} ${item.paused ? "paused" : ""}">
          <div class="session-head">
            <div>
              <div class="session-id">${esc(item.name || item.target_id || item.umo)}</div>
              <div class="session-name mono">${esc(item.umo)}</div>
            </div>
            <div class="inline" style="flex:none;gap:6px">${scopeTag}${statusTag}</div>
          </div>

          ${item.paused && item.paused_reason ? `<div class="banner warn" style="margin:11px 0 0">${esc(item.paused_reason)}</div>` : ""}

          <div class="session-metrics">
            <div class="metric">
              <div class="k">已静默</div>
              <div class="v">${item.quiet_for ? esc(item.quiet_for) : "刚说过话"}</div>
            </div>
            <div class="metric">
              <div class="k">下次开口</div>
              <div class="v countdown" ${item.eta ? `data-eta="${item.eta}"` : ""}>${item.eta ? "计算中" : "暂无"}</div>
            </div>
            <div class="metric">
              <div class="k">今日 / 累计</div>
              <div class="v">${esc(item.sent_today)} / ${esc(item.total_sent)}</div>
            </div>
            <div class="metric">
              <div class="k">记忆</div>
              <div class="v">${esc(item.memory_count)} 条</div>
            </div>
          </div>

          <div class="row-meta" style="margin-bottom:10px">
            <span>${esc(item.eta_why || "—")}</span>
            ${state.config?.trigger?.instant_enable
              ? `<span class="tag ${item.instant_probability !== item.instant_probability_global ? "acc" : ""}" title="${
                  item.instant_probability !== item.instant_probability_global
                    ? `本群单独设置，全局 ${item.instant_probability_global}%`
                    : "跟随全局设置"
                }">搭话 ${Math.round(item.instant_probability ?? 0)}%</span>`
              : ""}
            ${streak}
            ${item.pending_question ? '<span class="tag acc">等待接话</span>' : ""}
          </div>

          <div class="session-actions">
            <button class="btn tiny primary" data-act="preview" data-umo="${esc(item.umo)}">预览生成</button>
            <button class="btn tiny" data-act="trigger" data-umo="${esc(item.umo)}">立即发送</button>
            <button class="btn tiny" data-act="toggle" data-umo="${esc(item.umo)}" data-value="${item.enabled ? 0 : 1}">
              ${item.enabled ? "关闭" : "开启"}
            </button>
            ${
              item.paused
                ? `<button class="btn tiny" data-act="pause" data-umo="${esc(item.umo)}" data-value="0">恢复</button>`
                : `<button class="btn tiny" data-act="pause" data-umo="${esc(item.umo)}" data-value="1">暂停</button>`
            }
            <button class="btn tiny" data-act="prob" data-umo="${esc(item.umo)}" data-prob="${esc(item.instant_probability ?? "")}">搭话概率</button>
            <button class="btn tiny" data-act="rename" data-umo="${esc(item.umo)}" data-name="${esc(item.name)}">重命名</button>
            <button class="btn tiny" data-act="filter" data-umo="${esc(item.umo)}">看记忆</button>
            <button class="btn tiny danger" data-act="reset" data-umo="${esc(item.umo)}">清空上下文</button>
            <button class="btn tiny danger" data-act="delete" data-umo="${esc(item.umo)}">删除</button>
          </div>
        </div>`;
      })
      .join("")}
  </div>`;
}

/* ---- 记忆 ---- */
function viewMemory() {
  const options = state.sessions
    .map(
      (item) =>
        `<option value="${esc(item.umo)}" ${state.filterUmo === item.umo ? "selected" : ""}>${esc(
          item.name || item.target_id || item.umo
        )}</option>`
    )
    .join("");

  return `
    <div class="grid bento">
      <div class="card span-2">
        <h2>记忆库 <span class="hint">微光长期记住的关于这个群的事</span></h2>
        <div class="field">
          <label>选择会话</label>
          <select id="mem-umo">
            <option value="">全部会话</option>
            ${options}
          </select>
        </div>
        <div class="inline" style="margin-bottom:12px">
          <input type="text" id="mem-query" placeholder="搜索记忆内容…" value="${esc(state.memoryQuery)}" />
          <button class="btn" data-act="mem-search">搜索</button>
          <button class="btn" data-act="mem-clear">清空所选</button>
        </div>

        ${
          state.memories.length
            ? `<div class="rows">${state.memories
                .map(
                  (item) => `<div class="row">
              <div class="row-main">
                <div class="row-title">${esc(item.content)}</div>
                <div class="row-meta">
                  <span class="tag">${esc(item.kind || "fact")}</span>
                  <span>重要度 ${esc(item.importance)}</span>
                  <span>加入于 ${esc(item.created_text)}</span>
                  <span>命中 ${esc(item.hits)} 次</span>
                  <span class="mono">#${esc(item.id)}</span>
                </div>
              </div>
              <div class="row-actions">
                <button class="btn tiny" data-act="mem-edit" data-id="${item.id}" data-content="${esc(item.content)}">改</button>
                <button class="btn tiny danger" data-act="mem-del" data-id="${item.id}">删</button>
              </div>
            </div>`
                )
                .join("")}</div>`
            : `<div class="empty">还没有记忆。多聊几句，或者点右边的「立刻整理一次」让模型抽取。</div>`
        }
      </div>

      <div class="card">
        <h2>手动添加</h2>
        <p class="section-note">有些事模型不一定抓得到，可以直接告诉它。</p>
        <div class="field">
          <label>会话</label>
          <select id="mem-add-umo">
            <option value="">请选择会话</option>
            ${options}
          </select>
        </div>
        <div class="field">
          <label>内容</label>
          <textarea id="mem-add-content" placeholder="例如：小李下周三要去上海出差，别在那天约他"></textarea>
        </div>
        <div class="inline">
          <div class="field" style="margin:0">
            <label>重要度</label>
            <input type="number" id="mem-add-importance" value="0.8" min="0" max="1" step="0.05" />
          </div>
          <button class="btn primary" data-act="mem-add">记住它</button>
        </div>
      </div>

      <div class="card">
        <h2>整理</h2>
        <p class="section-note">
          抽取会调用一次模型。平时的抽取是攒够 ${esc(state.config?.memory?.extract_every ?? 30)} 条消息自动触发的，
          这里可以立刻手动跑一次。
        </p>
        <div class="field">
          <label>会话</label>
          <select id="mem-extract-umo">
            <option value="">请选择会话</option>
            ${options}
          </select>
        </div>
        <button class="btn primary" data-act="mem-extract">立刻整理一次</button>
      </div>
    </div>`;
}

/* ---- 定时 ---- */
function viewSchedule() {
  const options = state.sessions
    .map(
      (item) =>
        `<option value="${esc(item.umo)}" ${state.filterUmo === item.umo ? "selected" : ""}>${esc(
          item.name || item.target_id || item.umo
        )}</option>`
    )
    .join("");

  const DAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"];

  return `
    <div class="grid bento">
      <div class="card span-2">
        <h2>定时规则 <span class="hint">到点由微光结合上下文说一句，而不是发死文案</span></h2>
        ${
          state.schedules.length
            ? `<div class="rows">${state.schedules
                .map((item) => {
                  const days = (item.weekday_list || [])
                    .map((d) => DAY_NAMES[Number(d) - 1] || d)
                    .join(" ");
                  return `<div class="row">
                <div class="row-main">
                  <div class="row-title">${esc(item.name || "未命名")} · <span class="mono">${esc(item.at_time)}</span></div>
                  <div class="row-meta">
                    <span>每周 ${esc(days || "每天")}</span>
                    <span class="mono">${esc(item.umo)}</span>
                    ${item.brief ? `<span>${esc(item.brief)}</span>` : ""}
                    <span>上次执行 ${esc(item.last_run_date || "从未")}</span>
                    <span class="tag ${item.enabled ? "ok" : ""}">${item.enabled ? "启用" : "停用"}</span>
                  </div>
                </div>
                <div class="row-actions">
                  <button class="btn tiny" data-act="sch-toggle" data-id="${item.id}" data-value="${item.enabled ? 0 : 1}">
                    ${item.enabled ? "停用" : "启用"}
                  </button>
                  <button class="btn tiny danger" data-act="sch-del" data-id="${item.id}">删除</button>
                </div>
              </div>`;
                })
                .join("")}</div>`
            : `<div class="empty">还没有定时规则。定时问候是一个很轻的「打个招呼」入口，适合早安、晚安、周五提醒这类场景。</div>`
        }
      </div>

      <div class="card">
        <h2>新建规则</h2>
        <div class="field">
          <label>会话</label>
          <select id="sch-umo">
            <option value="">请选择会话</option>
            ${options}
          </select>
        </div>
        <div class="inline">
          <div class="field" style="margin:0">
            <label>时间</label>
            <input type="time" id="sch-time" value="09:00" />
          </div>
          <div class="field" style="margin:0">
            <label>名称</label>
            <input type="text" id="sch-name" placeholder="早安" />
          </div>
        </div>
        <div class="field">
          <label>星期</label>
          <div class="days" id="sch-days">
            ${DAY_NAMES.map(
              (name, index) =>
                `<button class="day on" data-day="${index + 1}">${name}</button>`
            ).join("")}
          </div>
        </div>
        <div class="field">
          <label>补充说明（可选）</label>
          <input type="text" id="sch-brief" placeholder="例如：语气轻松一点，别太正式" />
        </div>
        <button class="btn primary" data-act="sch-add">创建</button>
      </div>

      <div class="card">
        <h2>说明</h2>
        <p class="section-note">
          定时规则只负责「什么时候开口」，具体说什么仍然由模型结合群里的上下文与记忆决定，
          所以不会出现所有群收到同一句模板的情况。
        </p>
        <p class="section-note">
          错过超过 2 小时的规则不会补发，避免深夜把早上的问候补出来。
        </p>
      </div>
    </div>`;
}

/* ---- 日志 ---- */
function viewHistory() {
  const options = state.sessions
    .map(
      (item) =>
        `<option value="${esc(item.umo)}" ${state.filterUmo === item.umo ? "selected" : ""}>${esc(
          item.name || item.target_id || item.umo
        )}</option>`
    )
    .join("");

  return `
    <div class="grid bento">
      <div class="card span-2">
        <h2>主动消息记录 <span class="hint">每条都能看到为什么开口</span></h2>
        <div class="inline" style="margin-bottom:12px">
          <select id="his-umo">
            <option value="">全部会话</option>
            ${options}
          </select>
          <button class="btn" data-act="his-filter">筛选</button>
          <button class="btn danger" data-act="his-clear">清空记录</button>
        </div>
        ${
          state.history.length
            ? `<div class="timeline">${state.history
                .map((item) => {
                  const kind = !item.sent ? (/演练/.test(item.reason) ? "preview" : "failed") : "sent";
                  const stateText = !item.sent
                    ? /演练/.test(item.reason)
                      ? "演练（未发送）"
                      : "未发送"
                    : item.answered
                      ? "已发送 · 有人接话"
                      : "已发送";
                  return `<div class="tl-item ${kind}">
                <div class="tl-content">
                  <div class="row-meta">
                    <span class="tag ${kind === "sent" ? "ok" : kind === "failed" ? "bad" : ""}">
                      ${esc(TRIGGER_LABEL[item.trigger] || item.trigger)}
                    </span>
                    <span>${esc(stateText)}</span>
                    <span>${esc(item.time_text)}</span>
                  </div>
                  <div class="row-meta"><span>${esc(item.reason)}</span></div>
                  <div class="tl-quote">${esc(item.content)}</div>
                </div>
              </div>`;
                })
                .join("")}</div>`
            : `<div class="empty">还没有发送记录。可以到「会话」页点一次「预览生成」先看看效果。</div>`
        }
      </div>
    </div>`;
}

/* ---- 设置 ---- */
function field(label, key, value, note = "", type = "number", extra = "") {
  return `<div class="field">
    <label>${esc(label)}</label>
    <input type="${type}" data-cfg="${esc(key)}" value="${esc(value)}" ${extra} />
    ${note ? `<span class="note">${esc(note)}</span>` : ""}
  </div>`;
}

function toggleLine(label, key, value, sub = "") {
  return `<div class="switch-line">
    <div>
      <div class="label">${esc(label)}</div>
      ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}
    </div>
    <label class="switch">
      <input type="checkbox" data-cfg="${esc(key)}" data-kind="bool" ${value ? "checked" : ""} />
      <span class="track"><span class="knob"></span></span>
    </label>
  </div>`;
}

function viewSettings() {
  const c = state.config || {};
  const basic = c.basic || {};
  const iso = c.isolation || {};
  const ctx = c.context || {};
  const trg = c.trigger || {};
  const guard = c.guard || {};
  const mem = c.memory || {};
  const llm = c.llm || {};
  const adv = c.advanced || {};

  const providerOptions = [
    `<option value="" ${!llm.provider_id ? "selected" : ""}>跟随会话默认模型</option>`,
    ...state.providers.map(
      (item) =>
        `<option value="${esc(item.id)}" ${llm.provider_id === item.id ? "selected" : ""}>${esc(item.label)}</option>`
    ),
  ].join("");

  return `
    <div class="grid bento">
      <div class="card span-2">
        <h2>基础</h2>
        ${toggleLine("启用主动消息总开关", "basic.enabled", basic.enabled, "关掉后微光完全不会发言")}
        <div class="field" style="margin-top:12px">
          <label>生效群号列表</label>
          <input type="text" data-cfg="basic.group_whitelist" data-kind="list"
            value="${esc((basic.group_whitelist || []).join(","))}"
            placeholder="1001,1002 或填 all" />
          <span class="note">留空表示不生效；填 all 表示所有群。建议先只填一个测试群。</span>
        </div>
        ${toggleLine("允许私聊主动发消息", "basic.enable_private", basic.enable_private, "关闭时插件完全不读取私聊内容")}
      </div>

      <div class="card span-2">
        <h2>隔离与过滤 <span class="hint">与匿名树洞等插件共存的护栏</span></h2>
        ${toggleLine(
          "忽略机器人自己发出的消息",
          "isolation.ignore_self_sent",
          iso.ignore_self_sent,
          "匿名类插件是靠机器人账号把内容转发到群里的。开启后这些内容不会被统计成「群友在聊天」，也不会进入记忆库。建议保持开启。"
        )}
        <div class="field" style="margin-top:12px">
          <label>匿名昵称池</label>
          <input type="text" data-cfg="isolation.ignore_nicknames" data-kind="list"
            value="${esc((iso.ignore_nicknames || []).join(","))}"
            placeholder="番茄,苹果,橘子" />
          <span class="note">填你树洞插件里设置的昵称池，命中即跳过。</span>
        </div>
        <div class="field">
          <label>忽略正则</label>
          <input type="text" data-cfg="isolation.ignore_patterns" data-kind="list"
            value="${esc((iso.ignore_patterns || []).join(","))}"
            placeholder="^【.{1,8}】" />
          <span class="note">留空时按内置规则识别「【昵称】正文」格式。</span>
        </div>
        ${toggleLine("忽略以 / 开头的指令消息", "isolation.ignore_commands", iso.ignore_commands)}
      </div>

      <div class="card">
        <h2>触发 · 空闲唤醒</h2>
        ${toggleLine("启用", "trigger.idle_enable", trg.idle_enable)}
        ${field("静默多少分钟后触发", "trigger.idle_minutes", trg.idle_minutes, "分钟", "number", 'min="10"')}
      </div>

      <div class="card">
        <h2>触发 · 话题追问</h2>
        ${toggleLine("启用", "trigger.followup_enable", trg.followup_enable)}
        ${field("等待多少分钟后追问", "trigger.followup_minutes", trg.followup_minutes, "只追问一次，不纠缠", "number", 'min="5"')}
      </div>

      <div class="card">
        <h2>触发 · 随机关怀</h2>
        ${toggleLine("启用", "trigger.random_enable", trg.random_enable)}
        ${field("每日最少次数", "trigger.random_min_per_day", trg.random_min_per_day, "", "number", 'min="0"')}
        ${field("每日最多次数", "trigger.random_max_per_day", trg.random_max_per_day, "", "number", 'min="0"')}
      </div>

      <div class="card">
        <h2>触发 · 定时问候</h2>
        ${toggleLine("启用", "trigger.schedule_enable", trg.schedule_enable, "具体时刻在「定时」页编辑")}
      </div>

      <div class="card span-2">
        <h2>触发 · 即时搭话 <span class="hint">群友一发消息就掷骰子</span></h2>
        ${toggleLine(
          "启用",
          "trigger.instant_enable",
          trg.instant_enable,
          "开启后，每条群友消息都有机会让微光接一句。仍然受静默时段、每日上限与下面的冷却约束。"
        )}
        <div class="inline" style="margin-top:12px">
          ${field(
            "回复概率",
            "trigger.instant_probability",
            trg.instant_probability ?? 25,
            "百分比。0 = 不回，100 = 每条都回（不建议）",
            "number",
            'min="0" max="100"'
          )}
          ${field(
            "两套回复之间的最短间隔",
            "trigger.instant_cooldown_seconds",
            trg.instant_cooldown_seconds ?? 300,
            "秒。独立于全局冷却，防止群一热闹就连环刷屏",
            "number",
            'min="0"'
          )}
        </div>
        <div class="inline">
          ${field("延迟下限", "trigger.instant_delay_min", trg.instant_delay_min ?? 2, "秒", "number", 'min="0"')}
          ${field("延迟上限", "trigger.instant_delay_max", trg.instant_delay_max ?? 8, "秒，实际在这个区间里随机", "number", 'min="0"')}
        </div>
        <p class="section-note">
          即时搭话不看「距最后一条真人消息的最短间隔」——它就是要在大家聊天的时候接话。
          想让某个群更热闹或更安静，可以到「会话」页给那个群单独设概率。
        </p>
      </div>

      <div class="card span-2">
        <h2>防骚扰</h2>
        <div class="inline">
          ${field("静默时段开始", "guard.quiet_start", guard.quiet_start, "HH:MM", "time")}
          ${field("静默时段结束", "guard.quiet_end", guard.quiet_end, "HH:MM", "time")}
        </div>
        ${field("每个会话每日上限", "guard.max_per_day", guard.max_per_day, "所有触发方式合计", "number", 'min="1"')}
        ${field("两次主动消息的最小间隔", "guard.cooldown_minutes", guard.cooldown_minutes, "分钟", "number", 'min="0"')}
        ${field("距最后一条真人消息的最短间隔", "guard.min_human_gap_minutes", guard.min_human_gap_minutes, "分钟，防止大家在聊天时机器人硬插话", "number", 'min="0"')}
        ${field("多久之内有人说话算「被回应」", "guard.reply_window_minutes", guard.reply_window_minutes, "分钟", "number", 'min="1"')}
        ${field("连续无回应多少次后自动闭嘴", "guard.unanswered_pause", guard.unanswered_pause, "次，达到后该群自动暂停", "number", 'min="1"')}
      </div>

      <div class="card span-2">
        <h2>上下文与记忆</h2>
        ${field("参考最近多少条消息", "context.context_messages", ctx.context_messages, "条", "number", 'min="4"')}
        ${field("上下文有效期", "context.context_max_age_minutes", ctx.context_max_age_minutes, "分钟，太旧的历史不会硬拉回来", "number", 'min="30"')}
        ${toggleLine("把主动消息回写进对话上下文", "context.inject_into_context", ctx.inject_into_context, "开启后群友回复这条消息时，机器人记得自己说过什么")}
        ${toggleLine("启用长期记忆", "memory.enable", mem.enable)}
        ${field("每积累多少条消息抽取一次记忆", "memory.extract_every", mem.extract_every, "条，每次抽取会调用一次模型", "number", 'min="5"')}
        ${field("生成时最多注入多少条记忆", "memory.max_inject", mem.max_inject, "条", "number", 'min="0"')}
        ${field("记忆半衰期", "memory.decay_days", mem.decay_days, "天，越久远的记忆排序越靠后，0 表示不衰减", "number", 'min="0"')}
      </div>

      <div class="card span-2">
        <h2>模型与语气</h2>
        <div class="field">
          <label>使用的模型</label>
          <select data-cfg="llm.provider_id" data-kind="raw">${providerOptions}</select>
          <span class="note">留空则跟随当前会话的默认模型。</span>
        </div>
        <div class="field">
          <label>说话风格</label>
          <textarea data-cfg="llm.style">${esc(llm.style)}</textarea>
          <span class="note">写给模型的风格指令，可以按你的群氛围自由改。</span>
        </div>
        ${field("生成文案长度上限", "llm.max_chars", llm.max_chars, "字", "number", 'min="10"')}
        ${field("随机度", "llm.temperature", llm.temperature, "越高越活泼", "number", 'min="0" max="2" step="0.1"')}
      </div>

      <div class="card span-2">
        <h2>高级</h2>
        ${field("调度轮询间隔", "advanced.tick_seconds", adv.tick_seconds, "秒，轮询不消耗模型额度", "number", 'min="10"')}
        ${toggleLine("演练模式（只生成不发送）", "advanced.dry_run", adv.dry_run, "调参阶段建议先开一会儿，确认效果再关掉")}
        <div class="field">
          <label>日志级别</label>
          <select data-cfg="advanced.log_level" data-kind="raw">
            ${["debug", "info", "warning", "error"]
              .map((lv) => `<option value="${lv}" ${adv.log_level === lv ? "selected" : ""}>${lv}</option>`)
              .join("")}
          </select>
        </div>
      </div>
    </div>`;
}

/* --------------------------------------------------------------------------
   配置保存
   -------------------------------------------------------------------------- */
function buildPatch(path, value) {
  const parts = path.split(".");
  const patch = {};
  let cursor = patch;
  parts.forEach((part, index) => {
    if (index === parts.length - 1) {
      cursor[part] = value;
    } else {
      cursor[part] = {};
      cursor = cursor[part];
    }
  });
  return patch;
}

function readControl(node) {
  const kind = node.dataset.kind;
  if (kind === "bool") return node.checked;
  if (kind === "list") {
    return node.value
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  if (kind === "raw") return node.value;
  if (node.type === "number") {
    const num = Number(node.value);
    return Number.isFinite(num) ? num : 0;
  }
  return node.value;
}

async function saveControl(node) {
  const path = node.dataset.cfg;
  if (!path) return;
  const value = readControl(node);
  const result = await guard(() => post("config", buildPatch(path, value)));
  if (!result) return;
  state.config = result.config || state.config;
  state.warnings = result.warnings || [];
  await refreshOverview();
  // 只更新受影响的局部，避免整页重绘打断输入
  renderStats();
  renderTopbar();
  const card = node.closest(".card");
  if (state.tab === "overview" && card) loadTab(true);
}

/* --------------------------------------------------------------------------
   倒计时
   -------------------------------------------------------------------------- */
let countdownTimer = null;

function startCountdown() {
  if (countdownTimer) clearInterval(countdownTimer);
  countdownTimer = setInterval(tickCountdown, 1000);
  tickCountdown();
}

function tickCountdown() {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-eta]").forEach((node) => {
    const eta = Number(node.dataset.eta);
    if (!Number.isFinite(eta) || eta <= 0) return;
    const remain = eta - now;
    if (remain <= 0) {
      node.textContent = "就在此刻";
      return;
    }
    node.textContent = `还有 ${fmtDelta(remain)}`;
  });
}

/* --------------------------------------------------------------------------
   交互
   -------------------------------------------------------------------------- */
async function onAction(event) {
  const node = event.target.closest("[data-act]");
  if (!node) return;
  const act = node.dataset.act;
  if (state.busy && act !== "tab") return;

  const withBusy = async (fn, okMessage, reload = true) => {
    state.busy = true;
    node.disabled = true;
    const result = await guard(fn, okMessage);
    node.disabled = false;
    state.busy = false;
    if (result && reload) {
      await loadTab(true);
    }
    return result;
  };

  switch (act) {
    case "tab":
      state.tab = node.dataset.tab;
      renderTabs();
      $("#view").innerHTML = '<div class="skeleton"></div>';
      await guard(() => loadTab(true));
      renderView();
      break;

    case "tick":
      await withBusy(() => post("tick"), "已跑完一次判定", false);
      await loadTab(true);
      break;

    case "preview": {
      const umo = node.dataset.umo;
      const result = await withBusy(() => post("preview", { umo }), "", false);
      if (result) {
        if (result.ok) {
          showQuote("预览生成（未发送）", result.content);
        } else {
          toast(`生成失败：${result.error || "未知原因"}`, "err", 6000);
        }
      }
      break;
    }

    case "trigger": {
      const umo = node.dataset.umo;
      const result = await withBusy(() => post("trigger", { umo }), "", false);
      if (result) {
        if (result.sent) showQuote("已发送", result.content);
        else if (result.preview) showQuote("演练模式，未真正发送", result.content);
        else toast(`发送失败：${result.error || "未知原因"}`, "err", 6000);
      }
      break;
    }

    case "toggle":
      await withBusy(
        () => post("session", { umo: node.dataset.umo, enabled: node.dataset.value === "1" }),
        "已更新"
      );
      break;

    case "pause":
      await withBusy(
        () => post("session", { umo: node.dataset.umo, paused: node.dataset.value === "1" }),
        "已更新"
      );
      break;

    case "prob": {
      const current = node.dataset.prob ?? "";
      const next = window.prompt(
        "这个群单独的「即时搭话」概率（0~100）。\n留空或取消表示跟随全局设置"
        + (current ? `，当前 ${current}%` : "，当前跟随全局"),
        current
      );
      if (next === null) break;
      const value = next.trim();
      if (value !== "" && (!Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100)) {
        toast("概率要填 0~100 的数字", "err");
        break;
      }
      await withBusy(
        () => post("session", { umo: node.dataset.umo, instant_probability: value === "" ? null : Number(value) }),
        value === "" ? "已改为跟随全局概率" : `本群概率已设为 ${value}%`
      );
      break;
    }

    case "rename": {
      const next = window.prompt("给这个会话起个便于辨认的名字（留空则显示原始 ID）", node.dataset.name || "");
      if (next === null) break;
      await withBusy(() => post("session", { umo: node.dataset.umo, name: next }), "已重命名");
      break;
    }

    case "filter":
      state.filterUmo = node.dataset.umo;
      state.tab = "memory";
      renderTabs();
      await loadTab(true);
      renderView();
      break;

    case "reset":
      if (!(await askConfirm({ title: "清空上下文", message: "清空该会话的聊天上下文与发送记录？记忆会保留。", confirmText: "清空" }))) break;
      await withBusy(() => post("session/reset", { umo: node.dataset.umo }), "已清空");
      break;

    case "delete":
      if (!(await askConfirm({ title: "删除会话", message: "彻底删除该会话？上下文、记忆、发送记录都会一并删除，无法恢复。", confirmText: "删除" }))) break;
      await withBusy(() => post("session/delete", { umo: node.dataset.umo }), "已删除");
      break;

    case "mem-search":
      state.memoryQuery = $("#mem-query")?.value || "";
      state.filterUmo = $("#mem-umo")?.value || "";
      await loadTab(true);
      break;

    case "mem-add": {
      const umo = $("#mem-add-umo")?.value;
      const content = $("#mem-add-content")?.value || "";
      const importance = Number($("#mem-add-importance")?.value || 0.8);
      if (!umo) return toast("请先选择会话", "err");
      if (!content.trim()) return toast("请填写要记住的内容", "err");
      await withBusy(() => post("memory/add", { umo, content, importance }), "已记住");
      break;
    }

    case "mem-edit": {
      const next = window.prompt("修改这条记忆", node.dataset.content || "");
      if (next === null) break;
      await withBusy(() => post("memory/update", { id: Number(node.dataset.id), content: next }), "已保存");
      break;
    }

    case "mem-del":
      if (!(await askConfirm({ title: "删除记忆", message: "删除这条记忆？", confirmText: "删除" }))) break;
      await withBusy(() => post("memory/delete", { id: Number(node.dataset.id) }), "已删除");
      break;

    case "mem-clear": {
      const umo = $("#mem-umo")?.value;
      if (!umo) return toast("请在左侧选择一个具体会话后再清空", "err");
      if (!(await askConfirm({ title: "清空记忆", message: "清空该会话的全部记忆？", confirmText: "清空" }))) break;
      await withBusy(() => post("memory/clear", { umo }), "已清空");
      break;
    }

    case "mem-extract": {
      const umo = $("#mem-extract-umo")?.value;
      if (!umo) return toast("请先选择会话", "err");
      const result = await withBusy(() => post("memory/extract", { umo }), "", false);
      if (result) {
        if (result.extracted) toast(`整理完成，新增或更新了 ${result.extracted} 条记忆`, "ok");
        else toast(result.error || "这次没有抽取到新记忆", "info", 5000);
        await loadTab(true);
      }
      break;
    }

    case "sch-add": {
      const umo = $("#sch-umo")?.value;
      const atTime = $("#sch-time")?.value || "";
      const days = Array.from(document.querySelectorAll("#sch-days .day.on")).map((d) => Number(d.dataset.day));
      if (!umo) return toast("请先选择会话", "err");
      if (!atTime) return toast("请选择时间", "err");
      if (!days.length) return toast("至少选择一天", "err");
      await withBusy(
        () =>
          post("schedule/add", {
            umo,
            at_time: atTime,
            name: $("#sch-name")?.value || "",
            brief: $("#sch-brief")?.value || "",
            weekdays: days,
            enabled: true,
          }),
        "已创建"
      );
      break;
    }

    case "sch-toggle":
      await withBusy(
        () => post("schedule/update", { id: Number(node.dataset.id), enabled: node.dataset.value === "1" }),
        "已更新"
      );
      break;

    case "sch-del":
      if (!(await askConfirm({ title: "删除定时规则", message: "删除这条定时规则？", confirmText: "删除" }))) break;
      await withBusy(() => post("schedule/delete", { id: Number(node.dataset.id) }), "已删除");
      break;

    case "his-filter":
      state.filterUmo = $("#his-umo")?.value || "";
      await loadTab(true);
      break;

    case "his-clear":
      if (!(await askConfirm({ title: "清空发送记录", message: "清空发送记录？记忆与上下文不受影响。", confirmText: "清空" }))) break;
      await withBusy(() => post("history/clear", { umo: $("#his-umo")?.value || "" }), "已清空");
      break;

    default:
      break;
  }
}

function showQuote(title, text) {
  const old = $("#quotebox");
  if (old) old.remove();
  const box = document.createElement("div");
  box.id = "quotebox";
  box.className = "banner ok";
  box.style.marginTop = "12px";
  box.innerHTML = `<strong>${esc(title)}</strong>
    <div style="flex:1">${esc(text)}</div>
    <button class="btn tiny" data-close="1">关闭</button>`;
  $("#view").prepend(box);
  box.querySelector("[data-close]").addEventListener("click", () => box.remove());
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* --------------------------------------------------------------------------
   启动
   -------------------------------------------------------------------------- */
async function boot() {
  initNet();

  if (!bridge) {
    $("#view").innerHTML = `<div class="empty">
      这个页面需要从 AstrBot 的插件详情页打开。<br>
      直接访问文件时拿不到 bridge，无法读取数据。
    </div>`;
    return;
  }

  try {
    const context = await bridge.ready();
    if (context?.isDark !== undefined) {
      document.documentElement.dataset.theme = context.isDark ? "dark" : "light";
    }
    bridge.onContext?.((ctx) => {
      if (ctx?.isDark !== undefined) {
        document.documentElement.dataset.theme = ctx.isDark ? "dark" : "light";
      }
    });
  } catch (error) {
    /* bridge 未就绪时继续，接口调用本身也会给出错误 */
  }

  document.addEventListener("click", onAction);
  document.addEventListener("change", (event) => {
    const node = event.target.closest("[data-cfg]");
    if (node) saveControl(node);
  });
  document.addEventListener("click", (event) => {
    const day = event.target.closest("#sch-days .day");
    if (day) day.classList.toggle("on");
  });

  $("#master-switch").addEventListener("change", async (event) => {
    const result = await guard(() => post("config", { basic: { enabled: event.target.checked } }));
    if (!result) {
      event.target.checked = !event.target.checked;
      return;
    }
    state.config = result.config || state.config;
    state.warnings = result.warnings || [];
    await refreshOverview();
    renderAll();
  });

  $("#btn-refresh").addEventListener("click", async () => {
    await guard(() => loadBootstrap(), "已刷新");
  });

  $("#view").innerHTML = '<div class="skeleton"></div>';
  await guard(async () => {
    await loadBootstrap();
    await loadTab();
    renderAll();
  });
}

boot();
