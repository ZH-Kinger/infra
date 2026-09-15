// 云账号资产：员工看自己有子账号的云账号里各类资源的数量；管理员看明细、可搜索。

import { api, h, mount, platformTag } from "./core.js";

// CSP 不允许 style 属性，宽度走 CSSOM
function fill(percent) {
  const el = h("span", { class: "bar-fill" });
  el.style.width = `${percent}%`;
  return el;
}

function bars(items, key, total) {
  const max = Math.max(1, ...items.map((x) => x.count));
  return h(
    "ul",
    { class: "bars" },
    items.map((x) =>
      h(
        "li",
        {},
        h("span", { class: "bar-label", title: x[key] }, x[key]),
        h("span", { class: "bar-track" }, fill(Math.max(2, (x.count / max) * 100))),
        h("span", { class: "bar-n" }, String(x.count)),
      ),
    ),
    total > 0 ? null : h("li", { class: "muted" }, "没有资源"),
  );
}

function capturedLine(iso) {
  if (!iso) return h("p", { class: "lede stale" }, "资产还没有采集。管理员运行 delivery assets collect 后这里会显示。");
  const t = new Date(iso);
  return h("p", { class: "lede" }, `数据采集于 ${Number.isNaN(t.getTime()) ? iso : t.toLocaleString("zh-CN", { hour12: false })}`);
}

export function renderAssets(ctx, { admin }) {
  return ctx.load(
    () => api(admin ? "/api/admin/assets" : "/api/assets"),
    (data) => mount(assetsPage(data, admin)),
  );
}

function assetsPage(data, admin) {
  const accounts = data.accounts || [];
  const nodes = [
    h("header", { class: "masthead" }, h("h1", {}, admin ? "云资产" : "云账号资产"), capturedLine(data.captured_at)),
  ];
  if (!admin) nodes.push(h("p", { class: "lede" }, "你有子账号的云账号里，各类资源的数量和地域分布。资源明细请联系管理员。"));
  if (!accounts.length) {
    nodes.push(h("div", { class: "card empty" }, h("h2", {}, admin ? "还没有资产数据" : "你还没有云账号"), h("p", {}, admin ? "开通两家的资源中心，并用只读身份运行 delivery assets collect。" : "需要云账号时从「申请」开始。")));
    return nodes;
  }
  for (const acc of accounts) {
    const card = h(
      "section",
      { class: "card asset-card" },
      h("div", { class: "asset-head" }, h("div", { class: "chips" }, platformTag(acc.platform), h("b", {}, acc.account_label)), h("span", { class: "asset-total" }, h("b", {}, acc.error && !acc.total ? "—" : String(acc.total)), " 个资源")),
      acc.error ? h("div", { class: "banner crit" }, admin ? `本次没采集完整：${acc.error}` : "这个云账号本次没采集完整，数量可能不准。") : null,
      acc.error && !acc.total ? null : h("div", { class: "asset-grid" }, h("div", {}, h("div", { class: "section-label" }, "按类型"), bars(acc.by_type.slice(0, 12), "type", acc.total)), h("div", {}, h("div", { class: "section-label" }, "按地域"), bars(acc.by_region.slice(0, 8), "region", acc.total))),
      admin && acc.resources && acc.resources.length ? resourceTable(acc.resources) : null,
    );
    nodes.push(card);
  }
  return nodes;
}

function resourceTable(resources) {
  const tbody = h("tbody");
  const count = h("span", { class: "muted" });
  const search = h("input", { class: "search", type: "search", placeholder: "搜索名称、ID、类型、地域、标签", "aria-label": "搜索资源" });
  const fill = () => {
    const q = search.value.trim().toLowerCase();
    const rows = resources.filter((r) => !q || [r.name, r.id, r.type_label, r.region, r.group, ...Object.entries(r.tags || {}).map(([k, v]) => `${k}=${v}`)].join(" ").toLowerCase().includes(q));
    tbody.replaceChildren(
      ...rows.slice(0, 500).map((r) => h("tr", {}, h("td", {}, r.type_label), h("td", {}, h("span", { class: "pname" }, r.name || "—"), h("span", { class: "pmail mono" }, r.id)), h("td", {}, r.region || "全局"), h("td", { class: "muted" }, Object.entries(r.tags || {}).map(([k, v]) => `${k}=${v}`).join("  ") || "—"))),
    );
    count.textContent = rows.length > 500 ? `显示前 500 个，共 ${rows.length} 个` : `${rows.length} 个`;
  };
  search.addEventListener("input", fill);
  fill();
  return h(
    "details",
    { class: "fold" },
    h("summary", {}, "资源明细"),
    h("div", { class: "toolbar pad" }, count, search),
    h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "类型"), h("th", {}, "名称 / ID"), h("th", {}, "地域"), h("th", {}, "标签"))), tbody)),
  );
}
