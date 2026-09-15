// 管理后台「系统状态」：上线还缺什么、数据旧不旧、有没有卡住的申请单。只读。
//
// 先给结论（几项要处理），再按分组列出每一项：现状 + 怎么修。严重的排在最前面。

import { api, fmtTime, h, mount } from "./core.js";

const LEVEL = {
  crit: ["要处理", "crit"],
  warn: ["需留意", "warn"],
  ok: ["正常", "good"],
  off: ["未开启", ""],
};
const ORDER = { crit: 0, warn: 1, off: 2, ok: 3 };

export function renderHealth({ load }) {
  return load(
    () => api("/api/admin/health"),
    (data) => mount(page(data)),
  );
}

function page(data) {
  const checks = data.checks || [];
  const s = data.summary || {};
  const verdict = s.crit ? ["crit", `${s.crit} 项必须处理，平台还不能正常开通`] : s.warn ? ["warn", `${s.warn} 项需要留意，其余正常`] : ["good", "全部正常"];
  const nodes = [
    h("header", { class: "masthead" }, h("div", { class: "masthead-row" }, h("h1", {}, "系统状态"), h("button", { type: "button", class: "btn ghost small push", onclick: () => location.reload() }, "重新检查")), h("p", { class: "lede" }, `检查于 ${fmtTime(data.checked_at)}。只读本地配置和快照，不调用云或飞书接口，不显示任何密钥。`)),
    h("div", { class: `banner ${verdict[0]}` }, h("b", {}, verdict[1])),
    h(
      "section",
      { class: "stats" },
      stat(s.crit || 0, "要处理", s.crit ? "crit" : ""),
      stat(s.warn || 0, "需留意", s.warn ? "warn" : ""),
      stat(s.ok || 0, "正常"),
      stat(s.off || 0, "未开启"),
    ),
  ];
  const groups = [];
  for (const c of checks) if (!groups.includes(c.group)) groups.push(c.group);
  // 有问题的分组排前面
  const worst = (g) => Math.min(...checks.filter((c) => c.group === g).map((c) => ORDER[c.level] ?? 9));
  groups.sort((a, b) => worst(a) - worst(b));
  for (const g of groups) {
    const items = checks.filter((c) => c.group === g).sort((a, b) => (ORDER[a.level] ?? 9) - (ORDER[b.level] ?? 9));
    nodes.push(
      h(
        "section",
        { class: "group" },
        h("div", { class: "group-label" }, g),
        h(
          "div",
          { class: "card list" },
          items.map((c) => {
            const [label, tone] = LEVEL[c.level] || LEVEL.off;
            return h(
              "div",
              { class: `health-row level-${c.level}` },
              h("span", { class: `pill ${tone}` }, label),
              h("div", { class: "opt-main" }, h("div", { class: "opt-name" }, c.title), h("p", { class: "opt-desc" }, c.detail), c.fix && c.level !== "ok" ? h("p", { class: "health-fix" }, h("b", {}, "怎么处理："), c.fix) : null),
            );
          }),
        ),
      ),
    );
  }
  return nodes;
}

function stat(n, label, tone) {
  return h("div", { class: `stat ${tone || ""}` }, h("span", { class: "n" }, String(n)), h("span", { class: "l" }, label));
}
