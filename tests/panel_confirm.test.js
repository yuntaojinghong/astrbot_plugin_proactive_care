/**
 * 微光面板「删除」按钮的回归测试。
 *
 * 复现的问题：面板在受限 iframe 里，宿主未授予 allow-modals 时
 * `window.confirm()` 会被静默拦掉并返回 false。原代码的删除/清空操作全靠它兜底：
 *
 *     case "delete":
 *       if (!window.confirm("彻底删除该会话？...")) break;   // 永远 break
 *       await withBusy(() => post("session/delete", ...));
 *
 * 于是「按钮点着没用，删除不了会话」。
 *
 * 本测试把 window.confirm 设成「一调用就抛异常」，模拟被宿主拦掉的情形，
 * 然后直接点击删除按钮，断言自绘确认框出现、且确认后真的发出了请求。
 *
 * 运行：node tests/panel_confirm.test.js   （需要 jsdom，cwd = 仓库根）
 */

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const REPO = process.env.REPO || path.resolve(__dirname, "..");
const APP = fs.readFileSync(path.join(REPO, "pages/dashboard/app.js"), "utf8");
const HTML = fs.readFileSync(path.join(REPO, "pages/dashboard/index.html"), "utf8");

let passed = 0;
const failures = [];

function check(label, cond, extra) {
  if (cond) {
    passed += 1;
    console.log(`PASS  ${label}`);
  } else {
    failures.push(label);
    console.log(`FAIL  ${label}${extra ? "   " + extra : ""}`);
  }
}

/* 记录所有发出的请求 */
const calls = [];

function makeBridge() {
  return {
    __setInitialContext() {},
    apiGet(endpoint, params) {
      calls.push({ kind: "get", endpoint, params });
      if (endpoint === "bootstrap") {
        return Promise.resolve({
          ok: true,
          config: {},
          overview: { sessions: 1, active: 1, sent_today: 0, memories: 0 },
          sessions: [{
            umo: "QQ:GroupMessage:1001",
            name: "测试群",
            enabled: 1,
            paused: 0,
            last_human_ts: 0,
            total_sent: 0,
          }],
        });
      }
      if (endpoint === "sessions") {
        // 注意字段名是 sessions（不是 items）—— 前端读 data.sessions
        return Promise.resolve({ ok: true, sessions: [{
          umo: "QQ:GroupMessage:1001",
          name: "测试群",
          enabled: 1,
          paused: 0,
          last_human_ts: 0,
          total_sent: 0,
        }] });
      }
      return Promise.resolve({ ok: true, sessions: [], items: [], memories: [] });
    },
    apiPost(endpoint, body) {
      calls.push({ kind: "post", endpoint, body });
      return Promise.resolve({ ok: true, message: "已删除" });
    },
  };
}

async function main() {
  const dom = new JSDOM(HTML, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "http://localhost/plugin-page/astrbot_plugin_proactive_care/dashboard/",
  });
  const { window } = dom;

  window.AstrBotPluginPage = makeBridge();
  window.matchMedia = window.matchMedia || (() => ({
    matches: false, addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {},
  }));

  // 面板背景有个粒子动画会调 canvas.getContext("2d")，jsdom 不实现它
  // （返回 null 会让动画在 setTransform 上炸掉）。这里给个空实现。
  const ctxStub = new Proxy({}, {
    get(_t, key) {
      if (key === "canvas") return { width: 800, height: 600 };
      if (key === "measureText") return () => ({ width: 0 });
      if (key === "createLinearGradient" || key === "createRadialGradient") {
        return () => ({ addColorStop() {} });
      }
      if (key === "getImageData") {
        return () => ({ data: new Uint8ClampedArray(4) });
      }
      return () => {};
    },
    set() { return true; },
  });
  window.HTMLCanvasElement.prototype.getContext = () => ctxStub;

  // 关键：模拟宿主拦掉原生 confirm —— 一调用就抛，绝不能让代码靠它兜底。
  let confirmCalled = 0;
  window.confirm = () => {
    confirmCalled += 1;
    throw new Error("blocked by sandbox (no allow-modals)");
  };

  window.eval(APP);
  // 等 boot() 里的请求链跑完
  await new Promise((r) => setTimeout(r, 120));

  const doc = window.document;

  // ---- 切到「会话」页签，让删除按钮渲染出来 ----
  const sessionTab = doc.querySelector('[data-act="tab"][data-tab="sessions"]')
    || doc.querySelector('[data-act="tab"]');
  if (sessionTab) {
    sessionTab.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 120));
  }

  const delBtn = doc.querySelector('[data-act="delete"]');
  check("渲染出删除按钮", !!delBtn, delBtn ? "" : "页面上找不到 [data-act=delete]");
  if (!delBtn) {
    return report();
  }

  // ---- 点击删除 ----
  delBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));

  const overlay = doc.querySelector(".modal-overlay");
  check("点击后出现自绘确认框（而不是依赖原生 confirm）", !!overlay);

  check("没有调用原生 window.confirm()", confirmCalled === 0,
    `被调用了 ${confirmCalled} 次`);

  if (!overlay) {
    return report();
  }

  const okBtn = overlay.querySelector("[data-ok]");
  check("确认框里有确认按钮", !!okBtn);
  check("确认框里显示了要删的会话", !!overlay.querySelector(".modal-msg"));

  // ---- 先测「取消」不应发出请求 ----
  const cancelBtn = overlay.querySelector("[data-cancel]");
  if (cancelBtn) {
    cancelBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 60));
    const deletedAfterCancel = calls.some((c) => c.endpoint === "session/delete");
    check("点取消不发出删除请求", !deletedAfterCancel);
    check("点取消后确认框关闭", !doc.querySelector(".modal-overlay"));
  }

  // ---- 再点一次并确认 ----
  delBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  const overlay2 = doc.querySelector(".modal-overlay");
  check("可以再次打开确认框", !!overlay2);
  if (overlay2) {
    const ok2 = overlay2.querySelector("[data-ok]");
    ok2.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 120));
    const del = calls.find((c) => c.endpoint === "session/delete");
    check("确认后真的发出了 session/delete 请求", !!del,
      JSON.stringify(calls.map((c) => c.endpoint)));
    if (del) {
      check("请求带上了正确的 umo",
        del.body && del.body.umo === "QQ:GroupMessage:1001",
        JSON.stringify(del.body));
    }
  }

  return report();
}

function report() {
  console.log("");
  console.log(`结果: ${passed} 通过, ${failures.length} 失败`);
  if (failures.length) {
    console.log("失败项:");
    failures.forEach((f) => console.log(`  - ${f}`));
  } else {
    console.log("结论: 删除按钮在原生 confirm 被拦掉的环境下仍然可用");
  }
  // 面板背景的粒子动画是 requestAnimationFrame 死循环，事件循环不会自然空转结束，
  // 所以这里直接以退出码结束进程。
  process.exit(failures.length ? 1 : 0);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
