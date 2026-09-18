// 云账号资产：员工看**指给自己**的资源明细 + 其余的数量分布；管理员看全部明细、可指派归属。
//
// 归属是人工记的（资源中心不告诉你一台机器是谁的），所以这一页有两件事要说清楚：
//   · 没指过的显示「未指定」，不按名字或创建时间猜 —— 猜错一次这张表就没人信了
//   · 员工看不到别人的资源明细，只看得到数量

import { api, apiPost, fill as fillEl, fmtTime, h, mount, openDrawer, platformTag } from "./core.js";

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

const HOLDING_KIND = {
  credential: "访问凭证",
  account: "云账号",
  permission: "云账号权限",
  resource: "资源",
};

/** 从申请单算出来的持有物。点一行跳到那张申请单，凭证的查看地址在审批评论里。 */
function holdingsCard(items) {
  const rows = items.map((it) => {
    const tr = h("tr", { class: "clickable", tabindex: "0", role: "button" },
      h("td", {}, HOLDING_KIND[it.kind] || it.kind),
      h("td", {}, h("span", { class: "pname" }, it.title || "—"), it.detail ? h("span", { class: "pmail" }, it.detail) : null),
      h("td", {}, platformTag(it.platform), h("span", { class: "muted" }, " ", it.account_label || it.account)),
      h("td", {}, it.expires_at ? fmtTime(it.expires_at) : h("span", { class: "muted" }, "长期")));
    const open = () => { location.hash = `request=${encodeURIComponent(it.request_id)}`; };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    return tr;
  });
  return h("section", { class: "card asset-card" },
    h("div", { class: "asset-head" },
      h("div", { class: "chips" }, h("b", {}, "通过面板拿到的")),
      h("span", { class: "asset-total" }, h("b", {}, String(items.length)), " 项")),
    h("div", { class: "scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "类型"), h("th", {}, "内容"), h("th", {}, "云账号"), h("th", {}, "到期"))),
      h("tbody", {}, ...rows))));
}

// 两种标记的措辞是**给持有人看的动作**，不是给运维看的状态词。
// 「该换了」要说清楚怎么换才不断服务，否则人会直接删掉旧的那把。
const KEY_FLAG = {
  rotate: ["该换了", "crit", "建得太久。找管理员发一把新的，两把并存几天、程序都切过去之后再停旧的。"],
  unused: ["没人用", "warn", "还要用吗？不用就请管理员停用 —— 停用是可逆的，随时能改回来。"],
};

function ageText(days) {
  if (days === null || days === undefined) return h("span", { class: "muted" }, "不详");
  if (days >= 365) return `${Math.floor(days / 365)} 年 ${days % 365} 天`;
  return `${days} 天`;
}

/** 自己的 AK 台账：每把建了多久、上次什么时候用过。 */
function keysCard(data) {
  const keys = data.keys || [];
  const uncollected = data.uncollected || [];
  // 「有账号但一把都没有」值得说一句（是好事）；「压根没有云账号」就别占地方了
  if (!keys.length && !uncollected.length && !data.accounts) return null;

  const rows = keys.map((k) => {
    const flags = (k.flags || []).map((f) => {
      const [label, tone] = KEY_FLAG[f] || [f, ""];
      return h("span", { class: `pill ${tone}` }, label);
    });
    if (!k.active) flags.push(h("span", { class: "pill" }, "已停用"));
    return h("tr", { class: k.active ? "" : "dim" },
      h("td", {}, h("span", { class: "mono" }, `${k.id}…`)),
      h("td", {}, h("span", { class: "pname" }, k.user), h("span", { class: "pmail" }, k.account_label || k.account)),
      h("td", {}, ageText(k.age_days), k.created ? h("span", { class: "pmail" }, k.created.slice(0, 10)) : null),
      h("td", {}, k.never_used
        ? h("span", { class: "warn-text" }, "从来没用过")
        : k.idle_days === null || k.idle_days === undefined
          ? h("span", { class: "muted" }, "不详")
          : `${k.idle_days} 天前`),
      h("td", {}, ...flags));
  });

  const advice = [...new Set(keys.flatMap((k) => k.flags || []))]
    .map((f) => KEY_FLAG[f] && h("p", { class: "opt-desc" }, h("b", {}, `${KEY_FLAG[f][0]}：`), KEY_FLAG[f][2]))
    .filter(Boolean);

  // **三态在这一层最容易被合回去**：后端分得清清楚楚（keys / uncollected / accounts），
  // 渲染时一个 `keys.length ? 表格 : "你没有密钥"` 就把「没采到」重新说成了「没有」。
  // 采集身份掉了 ram:ListAccessKeys 的那天，全员会同时看到那句假的安心话 ——
  // 而那正是最该被发现的状态。所以「一把都没采到」必须走自己的分支，连头上那个
  // 数字都不能写 0：写 0 就等于替云上回答了一个我们这次根本没问到的问题。
  const blind = !keys.length && uncollected.length;
  return h("section", { class: "card asset-card" },
    h("div", { class: "asset-head" },
      h("div", { class: "chips" }, h("b", {}, "你的访问密钥")),
      h("span", { class: "asset-total" }, h("b", {}, blind ? "?" : String(keys.length)), " 把")),
    h("p", { class: "pad muted" },
      `只显示密钥 ID 的前 8 位 —— 面板不保存、也拿不到完整密钥。超过 ${data.stale_days ?? 180} 天该换，超过 ${data.unused_days ?? 90} 天没用过该问还要不要。`),
    data.captured_at
      // 年龄和「多久没用」是拿请求时刻减快照里的时间算的。快照放旧了，一把昨天还在用的
      // 密钥会显示成「15 天前」，再配上「没人用，停掉吧」的建议 —— 有人真会去停一把在跑的
      ? h("p", { class: "pad muted" }, `依据 ${fmtTime(data.captured_at)} 的权限快照，晚于这个时间的使用记录还没采进来。`)
      : null,
    keys.length
      ? h("div", { class: "scroll" }, h("table", {},
          h("thead", {}, h("tr", {},
            h("th", {}, "密钥"), h("th", {}, "子账号"), h("th", {}, "建了多久"), h("th", {}, "最近用过"), h("th", {}, "状态"))),
          h("tbody", {}, ...rows)))
      : blind
        ? null
        : h("p", { class: "pad" }, "你名下没有长期访问密钥 —— 这是好事：临时凭证到期自己失效，没有需要惦记轮换的东西。"),
    advice.length ? h("div", { class: "pad" }, ...advice) : null,
    uncollected.length
      ? h("div", { class: "banner warn" },
          blind
            ? `这次一把都没采到（${uncollected.map((u) => u.user).join("、")}）。这**不是**说你没有密钥，是说这次没查到 —— 请管理员检查采集身份的 ram:ListAccessKeys 权限。`
            : `上面这份不完整：没采到 ${uncollected.map((u) => u.user).join("、")} 的密钥清单（不等于没有）。请管理员检查采集身份的 ram:ListAccessKeys 权限。`)
      : null);
}

// 导出只为可测：密钥卡那条「没采到 ≠ 没有」的规矩服务端用例抓不到（后端返回是对的），
// 只有在这一层渲染出来才看得见。同 requests.js 的 applyPage。
export function assetsPage(data, admin) {
  const accounts = data.accounts || [];
  const nodes = [
    h("header", { class: "masthead" }, h("h1", {}, admin ? "云资产" : "云账号资产"), capturedLine(data.captured_at)),
  ];
  if (!admin) {
    nodes.push(h("p", { class: "lede" }, "这里是你名下的东西：通过面板拿到的凭证、子账号、权限，以及管理员指给你的云资源。"));
    const holdings = data.holdings || [];
    // 放在最前面：这是唯一一份归属确定的数据（申请人就写在申请单里）。
    // 云上采来的资源要管理员一条条指归属，没指之前员工那边一定是空的
    if (holdings.length) nodes.push(holdingsCard(holdings));
    const keys = keysCard(data.keys || {});
    if (keys) nodes.push(keys);
  }
  if (!accounts.length) {
    // 资产快照（assets collect）和权限快照（inventory collect）是两套独立采集，
    // 前者没跑是常态。这时候上面可能已经列出了持有物和密钥 —— 密钥本身就是账号存在的
    // 证据，紧跟着再说一句「你还没有云账号」就是自相矛盾，看的人只会不信这一页。
    const denies = !admin && nodes.length > 2;
    nodes.push(h("div", { class: "card empty" },
      h("h2", {}, admin ? "还没有资产数据" : denies ? "还没采到你的云资源" : "你还没有云账号"),
      h("p", {}, admin
        ? "开通两家的资源中心，并用只读身份运行 delivery assets collect。"
        : denies
          ? "上面那些来自权限快照和面板发出的凭证。云上的机器和存储要等管理员跑一次资产采集才会出现在这里。"
          : "需要云账号时从「申请」开始。")));
    return nodes;
  }
  for (const acc of accounts) {
    // 员工看的是「我有什么」，管理员看的是「这个账号有什么」。
    // 两者的头条数字不该是同一个 —— 把全账号总量摆在员工页的头条，等于告诉他
    // 「这 6137 台机器和你有关」，而他名下可能一台都没有
    const mine = acc.resources || [];
    const headline = admin
      ? h("span", { class: "asset-total" }, h("b", {}, acc.error && !acc.total ? "—" : String(acc.total)), " 个资源")
      : h("span", { class: "asset-total" }, h("b", {}, String(mine.length)), " 个资源归你");
    const card = h(
      "section",
      { class: "card asset-card" },
      h("div", { class: "asset-head" }, h("div", { class: "chips" }, platformTag(acc.platform), h("b", {}, acc.account_label)), headline),
      acc.error ? h("div", { class: "banner crit" }, admin ? `本次没采集完整：${acc.error}` : "这个云账号本次没采集完整，数量可能不准。") : null,
      admin && acc.filtered ? h("p", { class: "pad muted" }, `另有 ${acc.filtered} 条不计入资产：调用记录，以及用户/组/角色/策略这些身份对象（它们在「权限」那一页）。`) : null,
      // 类型/地域分布是全账号的口径，只对管理员有意义。员工要的是自己那几台在哪、是什么，
      // 那些信息在下面的表里
      admin && !(acc.error && !acc.total)
        ? h("div", { class: "asset-grid" }, h("div", {}, h("div", { class: "section-label" }, "按类型"), bars(acc.by_type.slice(0, 12), "type", acc.total)), h("div", {}, h("div", { class: "section-label" }, "按地域"), bars(acc.by_region.slice(0, 8), "region", acc.total)))
        : null,
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
      // 说清楚「为什么是空的」和「怎么才能不空」。只写「没有资源」会让人以为是页面坏了
      const why = acc.unassigned
        ? `这个云账号里 ${acc.unassigned} 个资源还没指定归属，所以这里是空的`
        : "这个云账号里还没有指给你的资源";
      return h("div", { class: "pad muted" }, `${why}。名下应该有资源的话，找管理员指派。`);
    }
    return h("details", { class: "fold", open: "" },
      h("summary", {}, `指给你的资源（${resources.length} 个）`),
      h("div", { class: "scroll" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "类型"), h("th", {}, "名称 / ID"), h("th", {}, "地域"), h("th", {}, ""))),
        h("tbody", {}, ...resources.map((r) => row(r, acc, admin, reload))))));
  }
  if (!resources.length) return null;
  return adminTable(acc, resources, reload);
}

const CATEGORY_TABS = [["all", "全部"], ["compute", "计算"], ["storage", "存储"], ["other", "网络 / 其他"]];
const OWNED_TABS = [["all", "全部"], ["no", "未指定"], ["yes", "已指定"]];
//: 一次最多勾这么多。和服务端的 _OWNER_BATCH_MAX 对齐
const BATCH_MAX = 200;

/** 管理员的资源表：筛选 + 多选 + 批量指派。
 *
 * 为什么要批量：一个账号几百条资产，逐条开抽屉填邮箱没人做得下来，于是归属表永远是空的，
 * 而归属是空的话员工那一侧整页都没有意义。筛选同理 —— 找不到要指的那几条就无从下手。
 */
function adminTable(acc, resources, reload) {
  const picked = new Set();
  const state = { q: "", category: "all", owned: "all" };
  const tbody = h("tbody");
  const count = h("span", { class: "muted" });
  const search = h("input", { class: "search", type: "search", placeholder: "搜索名称、ID、类型、地域、归属人", "aria-label": "搜索资源" });
  const mail = h("input", { class: "input", type: "email", placeholder: "指给谁（公司邮箱，留空＝取消指派）" });
  const note = h("input", { class: "input", type: "text", placeholder: "备注（可选）" });
  const msg = h("p", { class: "muted" });
  const bar = h("div", { class: "toolbar pad", hidden: true });

  const chips = (items, key) => h("div", { class: "chips" }, ...items.map(([v, label]) => {
    const b = h("button", { type: "button", class: state[key] === v ? "chip active" : "chip",
      onclick: () => { state[key] = v; picked.clear(); render(); } }, label);
    return b;
  }));
  const catRow = chips(CATEGORY_TABS, "category");
  const ownRow = chips(OWNED_TABS, "owned");

  const matches = () => {
    const q = state.q.trim().toLowerCase();
    return resources.filter((r) => {
      if (state.category !== "all" && (r.category || "other") !== state.category) return false;
      if (state.owned === "no" && r.owner_email) return false;
      if (state.owned === "yes" && !r.owner_email) return false;
      if (!q) return true;
      return [r.name, r.id, r.type_label, r.region, r.group, r.owner_email, r.owner_name,
        ...Object.entries(r.tags || {}).map(([k, v]) => `${k}=${v}`)].join(" ").toLowerCase().includes(q);
    });
  };

  const assign = async () => {
    const ids = [...picked];
    if (!ids.length) return;
    msg.textContent = "保存中…";
    try {
      await apiPost("/api/admin/assets/owner", {
        platform: acc.platform, account: acc.account, ids,
        email: mail.value.trim(), note: note.value.trim(),
      });
      picked.clear();
      reload();
    } catch (e) { msg.textContent = e.message || "保存失败"; }
  };
  const go = h("button", { class: "btn small", type: "button", onclick: assign }, "指派选中的");
  const clear = h("button", { class: "btn ghost small", type: "button", onclick: () => { picked.clear(); render(); } }, "取消选择");
  fillEl(bar, h("b", {}, ""), mail, note, h("div", { class: "actions" }, go, clear), msg);

  function pickRow(r) {
    const box = h("input", { type: "checkbox", "aria-label": `选择 ${r.name || r.id}` });
    box.checked = picked.has(r.id);
    box.addEventListener("click", (e) => e.stopPropagation());
    box.addEventListener("change", () => {
      if (box.checked && picked.size >= BATCH_MAX) { box.checked = false; msg.textContent = `一次最多选 ${BATCH_MAX} 个`; return; }
      if (box.checked) picked.add(r.id); else picked.delete(r.id);
      syncBar();
    });
    const tr = row(r, acc, true, reload);
    tr.insertBefore(h("td", { class: "pick" }, box), tr.firstChild);
    return tr;
  }

  function syncBar() {
    bar.hidden = picked.size === 0;
    bar.firstChild.textContent = `已选 ${picked.size} 项`;
    if (picked.size) msg.textContent = "";
  }

  function render() {
    const rows = matches();
    const unassigned = rows.filter((r) => !r.owner_email).length;
    fillEl(tbody, ...rows.slice(0, 500).map(pickRow));
    count.textContent =
      (rows.length > 500 ? `显示前 500 个，共 ${rows.length} 个` : `${rows.length} 个`) +
      `（未指定 ${unassigned}）`;
    // 重画之后勾选状态要跟着走：筛选换了、已经不在列表里的那些就不该还算在选中里
    const visible = new Set(rows.map((r) => r.id));
    for (const id of [...picked]) if (!visible.has(id)) picked.delete(id);
    syncBar();
    fillEl(catRow, ...CATEGORY_TABS.map(([v, label]) => h("button", { type: "button", class: state.category === v ? "chip active" : "chip",
      onclick: () => { state.category = v; render(); } }, label)));
    fillEl(ownRow, ...OWNED_TABS.map(([v, label]) => h("button", { type: "button", class: state.owned === v ? "chip active" : "chip",
      onclick: () => { state.owned = v; render(); } }, label)));
  }
  search.addEventListener("input", () => { state.q = search.value; render(); });
  render();

  return h(
    "details",
    { class: "fold" },
    h("summary", {}, "资源明细"),
    h("div", { class: "toolbar pad" }, count, search),
    h("div", { class: "toolbar pad" }, h("span", { class: "section-label" }, "分类"), catRow, h("span", { class: "section-label" }, "归属"), ownRow),
    bar,
    h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { class: "pick" }, ""), h("th", {}, "类型"), h("th", {}, "名称 / ID"), h("th", {}, "地域"), h("th", {}, "归属"))), tbody)),
  );
}
