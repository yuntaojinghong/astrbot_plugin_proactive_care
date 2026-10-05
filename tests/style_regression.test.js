/* 面板样式回归检查（不依赖 jsdom 的布局能力）
 *
 * 为什么用「静态解析 CSS」而不是渲染截图：
 * jsdom 不算 flex 布局，也不应用 <link> 引来的样式表，
 * 所以「元素实际有多宽、有没有溢出」在 jsdom 里测不出来。
 * 但这类 bug 的成因是**规则本身写错**（选择器没覆盖到、优先级输了），
 * 把规则解析出来断言，正好能卡住它。
 *
 * 本次修的真实问题：
 *   .inline > * { flex: 1; min-width: 120px; }
 * 这条是给「一行几个输入框」用的。但会话卡片头部的两个徽章
 * （群聊 / 生效中）也是它的子元素，于是各被撑到 120px，
 * 两个就要 240px+，在 340px 宽的卡里溢出 —— 表现为右上角「生效中」错位。
 *
 * 关键点：元素上写了内联 style="flex:none"，但内联样式管不了 min-width，
 * 120px 的地板依然生效。所以必须在 CSS 里为 .tag 显式解掉 min-width。
 *
 * 跑法：node tests/style_regression.test.js
 */

const fs = require("fs");
const path = require("path");

const REPO = process.env.REPO || process.cwd();
const CSS = path.join(REPO, "pages/dashboard/style.css");
const APPJS = path.join(REPO, "pages/dashboard/app.js");

let pass = 0, fail = 0;
const out = [];
function check(label, cond, extra) {
  if (cond) { pass++; out.push(`  OK   ${label}`); }
  else { fail++; out.push(`  FAIL ${label}${extra !== undefined ? "   " + JSON.stringify(extra) : ""}`); }
}

/* ------------------------------------------------------------------ *
 *  一个够用的 CSS 解析器：把样式表拆成 [{selectors, decls, index}]
 * ------------------------------------------------------------------ */
function parseRules(css) {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, "");   // 去注释
  const rules = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  let i = 0;
  while ((m = re.exec(clean)) !== null) {
    const selectors = m[1].split(",").map((s) => s.trim()).filter(Boolean);
    const decls = {};
    for (const part of m[2].split(";")) {
      const idx = part.indexOf(":");
      if (idx === -1) continue;
      decls[part.slice(0, idx).trim().toLowerCase()] =
        part.slice(idx + 1).trim();
    }
    // 忽略 at-rule 的包裹层（@media 等）：这里只看规则本身
    if (selectors.some((s) => s.startsWith("@"))) { i++; continue; }
    rules.push({ selectors, decls, index: i++ });
  }
  return rules;
}

/**
 * 简化版优先级：id 100 / class 与属性 10 / 元素 1。
 * 这里的选择器都很简单，够用。
 * 返回 [优先级, 出现顺序]，用于判断"谁最终生效"。
 */
function specificity(sel) {
  const s = sel.replace(/:{1,2}[a-z-]+(\([^)]*\))?/g, "");   // 去伪类
  const ids = (s.match(/#[\w-]+/g) || []).length;
  const classes = (s.match(/\.[\w-]+/g) || []).length
    + (s.match(/\[[^\]]+\]/g) || []).length;
  const els = (s.replace(/[.#][\w-]+/g, " ").match(/[a-z]+/gi) || []).length;
  return ids * 100 + classes * 10 + els;
}

/** 找出对某个选择器最终生效的某条属性值（考虑优先级 + 顺序）。 */
function effective(rules, selector, prop) {
  let best = null;
  for (const r of rules) {
    if (!r.selectors.includes(selector)) continue;
    if (!(prop in r.decls)) continue;
    const spec = Math.max(...r.selectors.map(specificity));
    if (!best || spec > best.spec
        || (spec === best.spec && r.index > best.index)) {
      best = { spec, index: r.index, value: r.decls[prop] };
    }
  }
  return best;
}

const css = fs.readFileSync(CSS, "utf8");
const rules = parseRules(css);
const appjs = fs.readFileSync(APPJS, "utf8");

/* ------------------------------------------------------------------ *
 *  1. 徽章不能被 min-width:120px 撑开
 * ------------------------------------------------------------------ */
const base = effective(rules, ".inline > *", "min-width");
check(".inline > * 确实有 min-width（前提规则还在）",
      !!base && /120px/.test(base.value), base && base.value);

for (const sel of [".inline > .tag", ".inline > .badge"]) {
  const rule = effective(rules, sel, "min-width");
  check(`${sel} 显式解掉 min-width（否则徽章被撑到 120px）`,
        !!rule && /auto|0/.test(rule.value), rule && rule.value);
  const flex = effective(rules, sel, "flex");
  check(`${sel} 不参与平分宽度`, !!flex && /none/.test(flex.value),
        flex && flex.value);
}

/* 优先级必须真的赢过 .inline > * —— 光有规则不够，得算得出它生效 */
{
  const baseSpec = specificity(".inline > *");
  const tagSpec = specificity(".inline > .tag");
  check(`.inline > .tag 的优先级 (${tagSpec}) 高于 .inline > * (${baseSpec})`,
        tagSpec > baseSpec, { tagSpec, baseSpec });
}

/* 同理，按钮原本就解掉了，别在重构时丢掉 */
{
  const rule = effective(rules, ".inline > .btn", "min-width");
  check(".inline > .btn 仍然解掉 min-width", !!rule && /auto|0/.test(rule.value),
        rule && rule.value);
}

/* ------------------------------------------------------------------ *
 *  2. 会话卡头部：长 ID 不能把徽章挤出去
 * ------------------------------------------------------------------ */
{
  const shrink = effective(rules, ".session-head > div:first-child", "min-width");
  check("会话卡头部的文字块允许收缩（min-width:0），不会挤掉徽章",
        !!shrink && /0/.test(shrink.value), shrink && shrink.value);

  const inlineRule = effective(rules, ".session-head > .inline", "flex");
  check("头部徽章组不参与拉伸/收缩",
        !!inlineRule && /none/.test(inlineRule.value),
        inlineRule && inlineRule.value);
}

/* ------------------------------------------------------------------ *
 *  3. DOM 结构对齐：确认 .tag 真的是 .inline 的直接子元素
 *
 *  如果哪天改成包一层 div，上面那两条选择器就失效了 —— 这条用来发现
 *  「选择器还在、但已经不匹配实际结构」的情况。
 * ------------------------------------------------------------------ */
{
  const usesInlineTagWrapper =
    /class="inline"[^>]*>\s*\$\{scopeTag\}\$\{statusTag\}/.test(appjs)
    || /class="inline"[^>]*>\$\{[^}]*Tag\}/.test(appjs);
  check("app.js 里徽章确实是 .inline 的直接子元素（选择器匹配得上）",
        usesInlineTagWrapper,
        appjs.includes('class="inline"') ? "有 .inline，但结构变了？" : "找不到 .inline");
  check("会话卡头部确实用了 .session-head", appjs.includes('class="session-head"'));
  check("statusTag 会产出 .tag 元素", /class="tag/.test(appjs));
}

/* ------------------------------------------------------------------ *
 *  4. 防回归：确认修的是真问题，不是把规则删了了事
 * ------------------------------------------------------------------ */
{
  // .inline > * 这条还有别的用处（一行几个输入框），不该被删
  check("保留 .inline > * 的平分行为（其他表单还在用）",
        !!base && /120px/.test(base.value));
  // 修法是"给徽章开例外"，不是"整体降级"
  const wrap = effective(rules, ".inline", "flex-wrap");
  check(".inline 仍允许换行（窄屏不会硬挤）",
        !!wrap && /wrap/.test(wrap.value), wrap && wrap.value);
}

console.log(out.join("\n"));
console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);
