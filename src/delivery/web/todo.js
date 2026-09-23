// 管理后台「待办」：现在轮到你处理什么。
//
// 为什么要有这一页：后台有八页，每页回答一个问题，但没有一页回答「现在该干什么」。
// 离职待确认在 IAM 页、开通失败在申请页、无主账号在人员页和体检页各一份 ——
// 除非挨页翻，否则什么都不会被处理。
//
// 这一页**只读 + 跳转**，不搬任何写操作：动云的事仍然在各自的页面上做，
// 那里有完整的上下文和确认。待办页要做的只是「把人带到该去的地方」。
//
// 排序和分组在服务端算（delivery/todo.py）—— 飞书卡片和命令行要给出同一个结论。

import { api, h, mount } from "./core.js";

const API = "/api/admin/todo";

export function renderTodo({ load }) {
  return load(() => api(API), (data) => mount(page(data)));
}

function page(data) {
  const counts = data.counts || {};
  const nodes = [
    h("header", { class: "masthead" },
      h("h1", {}, "待办"),
      h("p", { class: "lede" }, data.headline || "")),
    freshness(data.freshness || {}),
  ];
  for (const err of data.errors || []) {
    nodes.push(h("div", { class: "banner warn" }, err));
  }
  for (const group of data.groups || []) {
    nodes.push(groupCard(group, data.fold_after || 5));
  }
  if (!(data.groups || []).length && !(data.errors || []).length) {
    nodes.push(h("section", { class: "card" }, h("p", { class: "pad muted" },
      "没有需要你处理的。新的事项会自己出现在这里，也会私聊你一张飞书卡片。")));
  }
  return h("div", {}, ...nodes);
}

// 数据可信度：下面所有结论都建立在这几份数据上。哪一份旧了要说出来，
// 否则「没发现问题」和「没查」在页面上长得一模一样。
function freshness(all) {
  const bits = Object.entries(all);
  if (!bits.length) return null;
  const parts = bits.map(([name, meta]) => h("span", { class: meta.stale ? "pill warn" : "pill" },
    `${name} ${meta.at ? ago(meta.at) : "没采到"}`));
  return h("section", { class: "card" },
    h("div", { class: "pad chips" }, h("span", { class: "muted" }, "结论依据："), ...parts));
}

function groupCard(group, foldAfter) {
  const items = group.items || [];
  const head = h("div", { class: "group-label" }, group.name, " ",
    h("span", { class: "muted" }, `${items.length} 项`));
  const shown = items.slice(0, foldAfter).map(row);
  const rest = items.slice(foldAfter);
  const body = [head, ...shown];
  if (rest.length) {
    const box = h("div", { hidden: true }, ...rest.map(row));
    const more = h("button", { type: "button", class: "btn ghost small" }, `还有 ${rest.length} 项`);
    more.addEventListener("click", () => { box.hidden = false; more.remove(); });
    body.push(box, h("div", { class: "pad" }, more));
  }
  return h("section", { class: "card" }, ...body);
}

function row(item) {
  const meta = [];
  if (item.source) meta.push(item.source_at ? `${item.source} ${ago(item.source_at)}` : item.source);
  if (item.age_days != null) meta.push(`挂了 ${item.age_days} 天`);
  // href 目前全是 todo.py 里的硬编码常量，但只要哪天有 collector 把数据里的值透传进来，
  // 这里就是个 javascript: 入口。加一层白名单不花钱
  const href = /^#[\w/?=&-]*$/.test(String(item.href || "")) ? item.href : "";
  const go = href ? h("a", { class: "btn tiny", href }, item.action || "去处理") : null;
  return h("div", { class: `todo-row${item.weak ? " weak" : ""}` },
    h("div", { class: "todo-main" },
      h("b", {}, item.title),
      h("p", { class: "todo-what" }, item.what),
      item.weak ? h("p", { class: "hint warn-text" }, item.weak) : null,
      meta.length ? h("p", { class: "hint" }, meta.join(" · ")) : null),
    go);
}

function ago(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return `${mins} 分钟前`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} 小时前`;
  return `${Math.round(mins / 60 / 24)} 天前`;
}
