// 云账号资产：员工看**指给自己**的资源明细 + 其余的数量分布；管理员看全部明细、可指派归属。
//
// 归属是人工记的（资源中心不告诉你一台机器是谁的），所以这一页有两件事要说清楚：
//   · 没指过的显示「未指定」，不按名字或创建时间猜 —— 猜错一次这张表就没人信了
//   · 员工看不到别人的资源明细，只看得到数量

import { api, apiPost, fill as fillEl, h, mount, openDrawer, platformTag } from "./core.js";

function fact(label, value, mono) {
  return h("div", { class: "perm" },
    h("span", { class: "muted" }, label),
    h("span", { class: mono ? "mono" : "" }, value || "—"));
}

// 点开一条资源：把快照里有的都摊开。管理员多一个「指给谁」。
function openResource(r, acc, admin, onSaved) {
  openDrawer("res-title", (close) => {
    const body = h("div", { class: "drawer-body" },
      fact("类型", r.type_label),
      fact("名称", r.name),
      fact("资源 ID", r.id, true),
      fact("地域", r.region || "全局"),
      fact("创建于", (r.created || "").replace("T", " ").replace("Z", " UTC")),
      fact("资源组", r.group, true),
      fact("归属", r.owner_email ? `${r.owner_name || ""} ${r.owner_email}`.trim() : "未指定"),
      r.owner_note ? fact("备注", r.owner_note) : null,
      r.owner_at ? fact("指派于", r.owner_at.replace("T", " ")) : null,
      Object.keys(r.tags || {}).length
        ? h("div", { class: "perm" }, h("span", { class: "muted" }, "标签"),
            h("div", {}, Object.entries(r.tags).map(([k, v]) => h("div", { class: "mono small" }, `${k}=${v}`))))
        : null,
    );
    const nodes = [h("h2", { id: "res-title" }, r.name || r.id), body];
    if (admin) {
      const mail = h("input", { class: "input", type: "email", placeholder: "指给谁（公司邮箱，留空＝取消指派）", value: r.owner_email || "" });
      const note = h("input", { class: "input", type: "text", placeholder: "备注（可选，比如用途）", value: r.owner_note || "" });
      const msg = h("p", { class: "muted" });
      const save = h("button", { class: "btn", type: "button", onclick: async () => {
        save.disabled = true; msg.textContent = "保存中…";
        try {
          await apiPost("/api/admin/assets/owner", {
            platform: acc.platform, account: acc.account, id: r.id,
            email: mail.value.trim(), note: note.value.trim(),
          });
          close(); onSaved();
        } catch (e) { msg.textContent = e.message || "保存失败"; save.disabled = false; }
      } }, "保存");
      nodes.push(h("div", { class: "drawer-foot" }, mail, note, h("div", { class: "actions" }, save, h("button", { class: "btn ghost", type: "button", onclick: close }, "取消")), msg));
    } else {
      nodes.push(h("div", { class: "drawer-foot" }, h("button", { class: "btn ghost", type: "button", onclick: close }, "关闭")));
    }
    return h("div", { class: "drawer-card" }, ...nodes);
  });
}

// CSP 不允许 style 属性，宽度走 CSSOM
function barFill(percent) {
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
        h("span", { class: "bar-track" }, barFill(Math.max(2, (x.count / max) * 100))),
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
  if (!admin) nodes.push(h("p", { class: "lede" }, "指给你的资源可以点开看明细；其余只给数量和地域分布。归属由管理员指定，没指过的显示「未指定」。"));
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
      resourceTable(acc, admin, () => renderAssets.reload && renderAssets.reload()),
    );
    nodes.push(card);
  }
  return nodes;
}

function row(r, acc, admin, reload) {
  const owner = r.owner_email
    ? h("span", {}, r.owner_name || r.owner_email)
    : h("span", { class: "muted" }, "未指定");
  const open = () => openResource(r, acc, admin, reload);
  const tr = h("tr", { class: "clickable", tabindex: "0", role: "button" },
    h("td", {}, r.type_label),
    h("td", {}, h("span", { class: "pname" }, r.name || "—"), h("span", { class: "pmail mono" }, r.id)),
    h("td", {}, r.region || "全局"),
    h("td", {}, owner));
  tr.addEventListener("click", open);
  tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  return tr;
}

function resourceTable(acc, admin, reload) {
  const resources = acc.resources || [];
  if (!admin) {
    // 员工版：只有指给他的那些。一条都没有时说清楚为什么，而不是空白
    if (!resources.length) {
      const why = acc.unassigned ? `这个云账号里 ${acc.unassigned} 个资源还没指定归属` : "这里还没有指给你的资源";
      return h("div", { class: "pad muted" }, `${why}。需要认领请联系管理员。`);
    }
    return h("details", { class: "fold", open: "" },
      h("summary", {}, `指给你的资源（${resources.length} 个）`),
      h("div", { class: "scroll" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "类型"), h("th", {}, "名称 / ID"), h("th", {}, "地域"), h("th", {}, ""))),
        h("tbody", {}, ...resources.map((r) => row(r, acc, admin, reload))))));
  }
  if (!resources.length) return null;
  const tbody = h("tbody");
  const count = h("span", { class: "muted" });
  const search = h("input", { class: "search", type: "search", placeholder: "搜索名称、ID、类型、地域、归属人", "aria-label": "搜索资源" });
  const renderRows = () => {
    const q = search.value.trim().toLowerCase();
    const rows = resources.filter((r) => !q || [r.name, r.id, r.type_label, r.region, r.group, r.owner_email, r.owner_name, ...Object.entries(r.tags || {}).map(([k, v]) => `${k}=${v}`)].join(" ").toLowerCase().includes(q));
    fillEl(tbody,
      ...rows.slice(0, 500).map((r) => row(r, acc, admin, reload)),
    );
    count.textContent = rows.length > 500 ? `显示前 500 个，共 ${rows.length} 个` : `${rows.length} 个`;
  };
  search.addEventListener("input", renderRows);
  renderRows();
  return h(
    "details",
    { class: "fold" },
    h("summary", {}, "资源明细"),
    h("div", { class: "toolbar pad" }, count, search),
    h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "类型"), h("th", {}, "名称 / ID"), h("th", {}, "地域"), h("th", {}, "归属"))), tbody)),
  );
}
