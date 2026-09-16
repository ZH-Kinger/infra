// 云权限面板前端。零依赖、零构建。
//
// 安全约定（改这个文件前先读）：
//   · 接口返回的任何字段只经 textContent / 属性赋值进入 DOM，绝不拼进 innerHTML。
//   · 身份和权限数据只存在内存里，不写 localStorage / sessionStorage。
//   · 非管理员永远不发 /api/admin/* 请求——不靠后端 403 兜底来「隐藏」页面。

import { ApiError, api, apiPost, clear, h, mount, platformTag, requestTitle, safeHttps, safePath } from "./core.js";
import { renderAssets } from "./assets.js";
import { renderHealth } from "./health.js";
import { renderIam } from "./iam.js";
import { permissionRoutes } from "./permissions.js";
import { requestRoutes } from "./requests.js";

const state = { session: null, loginUrl: "", peopleFilter: "all", peopleQuery: "", peopleCache: null, flash: null };

const FOLD_LIMIT = 8;

// ── 名册审核 ──────────────────────────────────────────────────────────────
const REVIEW_DONE = {
  confirm: "已确认对应",
  reject: "已驳回对应",
  assign: "已分配账号",
  service: "已标记为服务号",
  undo: "已撤销人工记录",
};

async function review(body, question, button, rerender) {
  if (question && !window.confirm(question)) return;
  if (button) button.disabled = true;
  try {
    await apiPost("/api/admin/review", body);
    state.flash = { tone: "good", text: `${REVIEW_DONE[body.op] || "已保存"}：${body.account}` };
  } catch (err) {
    if (err.status === 401) {
      state.session = null;
      return renderLogin();
    }
    state.flash = { tone: "crit", text: err.message };
  }
  rerender();
}

function flashBanner() {
  const f = state.flash;
  state.flash = null;
  return f ? h("div", { class: `banner ${f.tone}`, role: "status" }, f.text) : null;
}

function accountKey(a) {
  return `${a.platform}/${a.account}/${a.name}`;
}

// ── 通用状态视图 ──────────────────────────────────────────────────────────
function skeleton() {
  return h(
    "div",
    { class: "skeleton", "aria-hidden": "true" },
    h("div", { class: "sk sk-title" }),
    h("div", { class: "sk sk-card" }),
    h("div", { class: "sk sk-card" }),
  );
}

function errorView(err, retry) {
  if (err.status === 401) {
    state.session = null;
    renderLogin();
    return null;
  }
  const title = err.status === 403 ? "没有权限" : "加载失败";
  return h(
    "div",
    { class: "card empty" },
    h("h2", {}, title),
    h("p", {}, err.message),
    err.status === 403 ? null : h("button", { class: "btn small", onclick: retry }, "重试"),
  );
}

async function load(fetcher, render) {
  mount(skeleton());
  const attempt = async () => {
    mount(skeleton());
    try {
      render(await fetcher());
    } catch (err) {
      const view = errorView(err instanceof ApiError ? err : new ApiError(0, String(err)), attempt);
      if (view) mount(view);
    }
  };
  await attempt();
}

// ── 顶栏与路由 ────────────────────────────────────────────────────────────
function renderTopbar(space = "user") {
  const s = state.session;
  const authed = Boolean(s && s.authenticated);
  const isAdmin = authed && s.role === "admin";
  document.getElementById("tabs-user").hidden = !authed || space !== "user";
  document.getElementById("tabs-admin").hidden = !isAdmin || space !== "admin";
  document.getElementById("who").hidden = !authed;
  document.body.classList.toggle("admin-space", isAdmin && space === "admin");
  const brand = document.getElementById("brand-space");
  brand.hidden = !(isAdmin && space === "admin");
  if (!authed) return;
  document.getElementById("who-name").textContent = s.name || "未命名";
  const switcher = document.getElementById("space-switch");
  switcher.hidden = !isAdmin;
  switcher.textContent = space === "admin" ? "回到员工视图" : "管理后台";
  switcher.setAttribute("href", space === "admin" ? "#me" : "#admin/requests");
  document.getElementById("logout").setAttribute("href", safePath(s.logout_url, "/auth/logout"));
}

function markTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === name);
    if (tab.dataset.tab === name) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
}

function decode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return "";
  }
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  const [path, query = ""] = raw.split("?");
  if (path.startsWith("person=")) return { page: "person", key: decode(path.slice(7)), space: "admin" };
  if (path.startsWith("admin/request=")) return { page: "admin-request", key: decode(path.slice(14)), space: "admin" };
  if (path === "admin/requests") return { page: "admin-requests", filter: query || "open", space: "admin" };
  if (path === "admin/assets") return { page: "admin-assets", space: "admin" };
  if (path === "admin/policies") return { page: "admin-policies", space: "admin" };
  if (path === "admin/health") return { page: "admin-health", space: "admin" };
  if (path === "admin/iam") return { page: "admin-iam", space: "admin" };
  if (path === "assets") return { page: "assets", space: "user" };
  if (path === "admin") return { page: "admin", space: "admin" };
  if (path.startsWith("request=")) {
    const [id, flag] = path.slice(8).split("&");
    return { page: "request", key: decode(id), fresh: flag === "new=1", space: "user" };
  }
  if (path === "requests") return { page: "requests", filter: query || "open", space: "user" };
  if (path === "permissions") return { page: "permissions", space: "user" };
  if (path === "apply") return { page: "apply", space: "user" };
  if (path.startsWith("apply=")) return { page: "apply", key: decode(path.slice(6)), space: "user" };
  return { page: "me", space: "user" };
}

const pages = requestRoutes({ load, errorView, route: () => route() });
const permissionPages = permissionRoutes({ load, errorView });

function route() {
  if (!state.session || !state.session.authenticated) return renderLogin();
  const isAdmin = state.session.role === "admin";
  const { page, key, filter, fresh, space } = parseHash();
  if (space === "admin" && !isAdmin) {
    renderTopbar("user");
    markTab("me");
    return mount(
      h(
        "div",
        { class: "card empty" },
        h("h2", {}, "没有权限"),
        h("p", {}, "管理后台只对管理员开放。"),
        h("a", { class: "btn small", href: "#me" }, "回到我的云账号"),
      ),
    );
  }
  renderTopbar(space);
  if (page === "admin") {
    markTab("admin");
    return renderAdmin();
  }
  if (page === "person" && key) {
    markTab("admin");
    return load(
      () => api(`/api/admin/people/${encodeURIComponent(key)}`),
      (detail) => mount(personPage(detail, { admin: true })),
    );
  }
  if (page === "admin-health") {
    markTab("admin-health");
    return renderHealth({ load });
  }
  if (page === "admin-iam") {
    markTab("admin-iam");
    return renderIam({ load });
  }
  if (page === "admin-policies") {
    markTab("admin-policies");
    return permissionPages.renderAdminPolicies();
  }
  if (page === "admin-assets") {
    markTab("admin-assets");
    return renderAssets({ load }, { admin: true });
  }
  if (page === "assets") {
    markTab("assets");
    return renderAssets({ load }, { admin: false });
  }
  if (page === "admin-requests") {
    markTab("admin-requests");
    return pages.renderAdminList(filter);
  }
  if (page === "admin-request" && key) {
    markTab("admin-requests");
    return pages.renderDetail(key, { admin: true });
  }
  if (page === "permissions") {
    markTab("permissions");
    return permissionPages.renderPermissions();
  }
  if (page === "apply") {
    markTab("apply");
    return pages.renderApply(key);
  }
  if (page === "requests") {
    markTab("requests");
    return pages.renderMine(filter);
  }
  if (page === "request" && key) {
    markTab("requests");
    return pages.renderDetail(key, { admin: false, fresh });
  }
  markTab("me");
  return load(
    async () => {
      const [detail, reqs] = await Promise.all([api("/api/me"), api("/api/requests").catch(() => null)]);
      return { ...detail, requests: reqs ? reqs.requests || [] : null };
    },
    (detail) => mount(personPage(detail, { admin: false })),
  );
}

function renderLogin() {
  renderTopbar();
  // 会话过期时拿不到新的 login_url：回首页重新走一遍（飞书模式显示登录页，代理模式由代理跳 IAM）
  const url = safePath(state.loginUrl, "/");
  const known = url !== "/";
  const viaIam = url.startsWith("/oauth2/");
  mount(
    h(
      "div",
      { class: "card login" },
      h("h1", {}, "云权限面板"),
      h("p", {}, `查看你在阿里云、火山引擎上的账号和权限。${!known ? "" : viaIam ? "用公司 IAM 登录。" : "用飞书账号登录。"}`),
      h("a", { class: "btn", href: url }, !known ? "重新登录" : viaIam ? "用公司 IAM 登录" : "用飞书登录"),
    ),
  );
}

// ── 时间 ─────────────────────────────────────────────────────────────────
function snapshotLine(capturedAt) {
  if (!capturedAt) {
    return h("p", { class: "lede stale" }, "还没有权限快照，下面只显示映射表里的账号，看不到权限明细。");
  }
  const t = new Date(capturedAt);
  if (Number.isNaN(t.getTime())) return h("p", { class: "lede" }, `数据采集于 ${capturedAt}`);
  const hours = Math.max(0, (Date.now() - t.getTime()) / 36e5);
  const ago = hours < 1 ? "不到 1 小时前" : hours < 48 ? `${Math.floor(hours)} 小时前` : `${Math.floor(hours / 24)} 天前`;
  const text = `数据采集于 ${t.toLocaleString("zh-CN", { hour12: false })}（${ago}）`;
  return h(
    "p",
    { class: hours > 24 ? "lede stale" : "lede", title: capturedAt },
    hours > 24 ? `${text}，可能已过时` : text,
  );
}

function incompleteBanner(items) {
  if (!Array.isArray(items) || !items.length) return null;
  return h(
    "div",
    { class: "banner crit", role: "alert" },
    h("b", {}, "这份快照不完整。"),
    " 下面这些账号没采到，看不到权限不代表没有权限：",
    h("ul", {}, items.map((x) => h("li", {}, x))),
  );
}

// ── 个人详情（我的权限 / 管理员看某人共用） ────────────────────────────────

function policyList(policies, highRisk) {
  const risky = new Set(highRisk || []);
  const isRisk = (p) => risky.has(p) || [...risky].some((r) => r.startsWith(`${p}（`));
  const sorted = [...(policies || [])].sort((a, b) => Number(isRisk(b)) - Number(isRisk(a)));
  if (!sorted.length) return h("p", { class: "muted" }, "无");
  const list = h("ul", { class: "plist" });
  const items = sorted.map((p) => h("li", { class: isRisk(p) ? "risk" : "" }, p));
  const head = items.slice(0, FOLD_LIMIT);
  const rest = items.slice(FOLD_LIMIT);
  list.append(...head);
  if (!rest.length) return list;
  const wrap = h("div", {}, list);
  const more = h(
    "button",
    {
      class: "linkbtn",
      type: "button",
      onclick: () => {
        list.append(...rest);
        more.remove();
      },
    },
    `展开全部（还有 ${rest.length} 条）`,
  );
  wrap.append(more);
  return wrap;
}

// 这个子账号上通过申请开通、还没到期的权限（按到期时间排序），带续期入口
function grantsSection(acct, requests) {
  const now = Date.now();
  const grants = (requests || [])
    .filter((r) => r.status === "done" && r.kind === "permission" && r.expires_at && new Date(r.expires_at).getTime() > now)
    .filter((r) => r.template.platform === acct.platform && r.template.account === acct.account && (!r.payload || !r.payload.cloud_user || r.payload.cloud_user === acct.name))
    .sort((a, b) => new Date(a.expires_at) - new Date(b.expires_at));
  if (!grants.length) return null;
  const soon = now + 7 * 86400 * 1000;
  return h(
    "div",
    { class: "section" },
    h("div", { class: "section-label" }, "申请开通的权限", h("span", { class: "muted" }, String(grants.length))),
    h(
      "ul",
      { class: "grant-list" },
      grants.map((r) => {
        const expiring = new Date(r.expires_at).getTime() < soon;
        return h(
          "li",
          {},
          h("a", { class: r.template.id === "policy" ? "grant-name mono" : "grant-name", href: `#request=${encodeURIComponent(r.id)}` }, requestTitle(r)),
          h("span", { class: expiring ? "pill warn" : "pill" }, `${new Date(r.expires_at).toLocaleDateString("zh-CN")} 到期`),
          h("a", { class: "linkbtn", href: r.template.id === "policy" ? "#permissions" : `#apply=${encodeURIComponent(r.template.id)}` }, "续期"),
        );
      }),
    ),
  );
}

// 「进入控制台」：公司 IAM 发起的 SSO 登录，地址在 identity/accounts.json 里配，只放行 https
function consoleLink(acct) {
  const url = safeHttps(acct.console_url);
  return url
    ? h(
        "a",
        { class: "btn ghost small push", href: url, target: "_blank", rel: "noopener noreferrer" },
        "进入控制台 ↗",
      )
    : null;
}

function accountCard(acct, requests) {
  const gone = acct.in_snapshot === false;
  const highRisk = acct.high_risk || [];
  const head = h(
    "div",
    { class: "acct-head" },
    h(
      "div",
      { class: "acct-title" },
      h("div", { class: "chips" }, platformTag(acct.platform, acct.platform_display), h("span", { class: "muted" }, acct.account_label || acct.account || "")),
      h("span", { class: "acct-name" }, acct.name || ""),
      acct.display_name && acct.display_name !== acct.name ? h("span", { class: "acct-sub" }, acct.display_name) : null,
      acct.email ? h("span", { class: "acct-sub" }, `云上登记邮箱 ${acct.email}`) : null,
    ),
    highRisk.length ? h("span", { class: "pill crit" }, `高危 ${highRisk.length}`) : null,
    gone ? h("span", { class: "pill" }, "快照中不存在") : null,
    consoleLink(acct),
  );

  const sections = [];
  if (gone) {
    sections.push(
      h("div", { class: "section" }, h("p", { class: "muted" }, "映射表里登记了这个账号，但最新快照里没有它：可能已被删除，或这个云账号没有采集到。")),
    );
  } else {
    if (highRisk.length) {
      sections.push(
        h(
          "div",
          { class: "section" },
          h("div", { class: "section-label" }, "高危权限"),
          h("ul", { class: "plist" }, highRisk.map((p) => h("li", { class: "risk" }, p))),
        ),
      );
    }
    const grants = grantsSection(acct, requests);
    if (grants) sections.push(grants);
    sections.push(
      h(
        "div",
        { class: "section" },
        h("div", { class: "section-label" }, "所在组"),
        (acct.groups || []).length
          ? h("div", { class: "chips" }, acct.groups.map((g) => h("span", { class: "pill" }, g)))
          : h("p", { class: "muted" }, "不在任何组"),
      ),
      h(
        "div",
        { class: "section" },
        h("div", { class: "section-label" }, "直接授予", h("span", { class: "muted" }, String((acct.direct_policies || []).length))),
        policyList(acct.direct_policies, highRisk),
      ),
    );
    for (const block of acct.group_policies || []) {
      sections.push(
        h(
          "div",
          { class: "section" },
          h("div", { class: "section-label" }, `经组继承 · ${block.group || ""}`, h("span", { class: "muted" }, String((block.policies || []).length))),
          policyList(block.policies, highRisk),
        ),
      );
    }
  }
  return h("article", { class: gone ? "card acct gone" : "card acct" }, head, ...sections);
}

function personPage(detail, { admin }) {
  const person = detail.person || {};
  const summary = detail.summary || {};
  const accounts = detail.accounts || [];
  const nodes = [];

  const titleRow = h("div", { class: "masthead-row" });
  if (admin) {
    titleRow.append(h("a", { class: "btn ghost small", href: "#admin" }, "← 返回人员总览"));
  }
  titleRow.append(h("h1", {}, admin ? person.name || "未命名" : "我的云账号"));
  if (admin && detail.binding !== "union_id") {
    titleRow.append(h("span", { class: "pill warn" }, "未绑定 union_id"));
  }
  nodes.push(
    h(
      "header",
      { class: "masthead" },
      titleRow,
      person.email ? h("p", { class: "lede" }, person.email) : null,
      snapshotLine(detail.captured_at),
    ),
  );

  nodes.push(incompleteBanner(detail.snapshot_incomplete));
  if (admin) nodes.push(flashBanner());

  if (!admin && detail.binding === "bound_now") {
    nodes.push(h("div", { class: "banner good" }, "已按企业邮箱关联到你的账号并记录 union_id。之后登录只按 union_id 识别。"));
  }

  if (admin && detail.binding === "unbound") {
    // 管理员视角：未绑定只是状态，账号照常列出
    nodes.push(h("div", { class: "banner warn" }, detail.binding_note || "此人还没有绑定 union_id。"));
  } else if (detail.binding === "unbound" || detail.binding === "conflict") {
    nodes.push(
      h(
        "div",
        { class: "card empty" },
        h("h2", {}, detail.binding === "conflict" ? "身份关联冲突" : "还没有关联到云账号"),
        h("p", {}, detail.binding_note || "请联系管理员登记。"),
        person.union_id ? h("p", { class: "muted mono" }, `你的 union_id：${person.union_id}`) : null,
        detail.binding === "unbound"
          ? h(
              "div",
              { class: "empty-split" },
              h("div", {}, h("b", {}, "新同事，还没有云账号？"), h("p", { class: "muted" }, "直接申请开子账号，审批通过后自动开通并对应到你名下。")),
              h("a", { class: "btn small", href: "#apply" }, "申请开子账号"),
            )
          : null,
      ),
    );
    return nodes;
  }

  const pending = detail.pending || [];
  if (!accounts.length && !pending.length) {
    nodes.push(
      h(
        "div",
        { class: "card empty" },
        h("h2", {}, admin ? "没有阿里云或火山账号" : "你目前没有阿里云或火山账号"),
        h("p", {}, admin ? "这个人在已采集的云账号里没有对应的号。" : "先申请开一个子账号。审批通过后自动开通，可以领取控制台初始密码；之后就能在「权限列表」里申请权限。"),
        admin ? null : h("a", { class: "btn small", href: "#apply" }, "申请开子账号"),
      ),
    );
    return nodes;
  }

  nodes.push(
    h(
      "section",
      { class: "stats" },
      stat(summary.account_count, "云账号"),
      stat(summary.platforms, "平台"),
      stat(summary.policy_count, "权限条数"),
      stat(summary.high_risk_count, "高危权限", summary.high_risk_count ? "crit" : ""),
    ),
  );

  if (!admin && detail.requests) nodes.push(reminders(detail.requests));

  if (accounts.length) {
    nodes.push(
      h(
        "section",
        { class: "group" },
        h("div", { class: "group-head" }, h("div", { class: "group-label" }, "云账号"), admin ? null : h("a", { class: "linkbtn push", href: "#permissions" }, "申请更多权限 →")),
        h("div", { class: "accounts" }, accounts.map((a) => accountCard(a, admin ? null : detail.requests))),
      ),
    );
  }
  if (pending.length) {
    nodes.push(
      h(
        "section",
        { class: "group" },
        h("div", { class: "group-label" }, "待确认对应"),
        h("div", { class: "accounts" }, pending.map((item) => pendingCard(item, admin ? person : null))),
      ),
    );
  }
  return nodes;
}

// 首页提醒：能领取的、等审批的、快到期的。没有就不显示。
function reminders(requests) {
  const soon = Date.now() + 7 * 86400 * 1000;
  const items = [];
  for (const r of requests) {
    const link = `#request=${encodeURIComponent(r.id)}`;
    if (r.actions && r.actions.credential) items.push(["good", "可领取", `${requestTitle(r)}：临时凭证已批准，可以领取`, link, "去领取"]);
    else if (r.actions && r.actions.password) items.push(["good", "可领取", `${requestTitle(r)}：初始密码可以领取`, link, "去领取"]);
    else if (r.status === "pending_approval") items.push(["accent", "待审批", `${requestTitle(r)}：等审批人在飞书里处理`, link, "查看"]);
    else if (r.status === "failed") items.push(["crit", "开通失败", `${requestTitle(r)}：管理员会处理`, link, "查看"]);
    else if (r.status === "done" && r.expires_at && new Date(r.expires_at).getTime() < soon) {
      items.push(["warn", "快到期", `${requestTitle(r)}：${new Date(r.expires_at).toLocaleDateString("zh-CN")} 到期，需要继续用请续期`, r.template.id === "policy" ? "#permissions" : `#apply=${encodeURIComponent(r.template.id)}`, "续期"]);
    }
  }
  if (!items.length) return null;
  const order = { good: 0, crit: 1, warn: 2, accent: 3 };
  items.sort((a, b) => order[a[0]] - order[b[0]]);
  return h(
    "section",
    { class: "group" },
    h("div", { class: "group-head" }, h("div", { class: "group-label" }, "需要留意"), h("a", { class: "linkbtn push", href: "#requests" }, "全部申请 →")),
    h(
      "div",
      { class: "card list" },
      items.slice(0, 6).map(([tone, label, text, href, cta]) => h("a", { class: "row", href }, h("div", { class: "row-main" }, h("div", { class: "row-title" }, h("span", { class: `pill ${tone}` }, label), h("span", {}, text))), h("div", { class: "row-side" }, h("span", { class: "linkbtn" }, `${cta} →`)))),
    ),
  );
}

const PENDING_STATUS = { review: "待人工确认", blocked: "受阻" };

function pendingCard(item, person) {
  let actions = null;
  if (person && person.email) {
    const body = (op) => ({ op, email: person.email, account: accountKey(item) });
    const who = person.name || person.email;
    actions = h(
      "div",
      { class: "section review-actions" },
      h(
        "button",
        {
          type: "button",
          class: "btn small",
          onclick: (e) => review(body("confirm"), `确认 ${item.name} 是 ${who} 的账号？确认后会进入 IAM 属性表。`, e.currentTarget, route),
        },
        "确认是此人",
      ),
      h(
        "button",
        {
          type: "button",
          class: "btn small ghost",
          onclick: (e) => review(body("reject"), `${item.name} 不是 ${who} 的账号？以后刷新也不再推给此人。`, e.currentTarget, route),
        },
        "不是此人",
      ),
    );
  } else if (person) {
    actions = h("div", { class: "section" }, h("p", { class: "muted" }, "此人没有邮箱，暂不能在面板上处理。"));
  }
  return h(
    "article",
    { class: "card acct pending" },
    h(
      "div",
      { class: "acct-head" },
      h(
        "div",
        { class: "acct-title" },
        h("div", { class: "chips" }, platformTag(item.platform, item.platform_display), h("span", { class: "muted" }, item.account_label || item.account || "")),
        h("span", { class: "acct-sub" }, `待确认对应：${item.name || ""}（${PENDING_STATUS[item.status] || item.status || "待确认"}）`),
      ),
      h("span", { class: item.status === "blocked" ? "pill crit" : "pill warn" }, PENDING_STATUS[item.status] || item.status || "待确认"),
    ),
    h("div", { class: "section" }, h("p", { class: "muted" }, "映射尚未确认，不展示这个账号的权限。")),
    actions,
  );
}

function stat(value, label, tone) {
  return h(
    "div",
    { class: tone ? `stat ${tone}` : "stat" },
    h("span", { class: "n" }, value === undefined || value === null ? "—" : String(value)),
    h("span", { class: "l" }, label),
  );
}

// ── 人员总览 ──────────────────────────────────────────────────────────────
const FILTERS = [
  ["all", "全部"],
  ["multi", "多账号"],
  ["high_risk", "高危"],
  ["unbound", "未绑定 union_id"],
  ["no_account", "无云账号"],
];

async function renderAdmin() {
  mount(skeleton());
  let overview;
  let people;
  let records;
  const retry = () => renderAdmin();
  try {
    [overview, people, records] = await Promise.all([
      api("/api/admin/overview"),
      api(`/api/admin/people?filter=${encodeURIComponent(state.peopleFilter)}`),
      // 审核数据坏了不能拖垮整个总览页：只是不显示审核功能
      api("/api/admin/review").catch((err) => ({ enabled: false, records: [], error: err.message })),
    ]);
  } catch (err) {
    const view = errorView(err instanceof ApiError ? err : new ApiError(0, String(err)), retry);
    if (view) mount(view);
    return;
  }
  state.peopleCache = people;
  mount(adminPage(overview, people, records));
}

function adminPage(overview, people, records) {
  const totals = overview.totals || {};
  const nodes = [
    h("header", { class: "masthead" }, h("h1", {}, "人员总览"), snapshotLine(overview.captured_at)),
    flashBanner(),
    incompleteBanner(overview.snapshot_incomplete),
  ];
  for (const w of overview.warnings || []) nodes.push(h("div", { class: "banner warn" }, w));

  nodes.push(
    h(
      "section",
      { class: "stats" },
      stat(totals.people, "人"),
      stat(totals.cloud_users, "云上用户"),
      stat(totals.multi_account_people, "多账号的人", totals.multi_account_people ? "warn" : ""),
      stat(totals.high_risk_people, "有高危权限的人", totals.high_risk_people ? "crit" : ""),
      stat(totals.unbound_people, "未绑定 union_id", totals.unbound_people ? "warn" : ""),
      stat(totals.unlinked_accounts, "未关联到人的账号", totals.unlinked_accounts ? "warn" : ""),
      stat(totals.no_account_people, "无云账号的人"),
    ),
  );

  const acctRows = (overview.accounts || []).map((a) =>
    h(
      "tr",
      {},
      h("td", {}, platformTag(a.platform, a.platform_display), " ", h("span", {}, a.account_label || a.account || "")),
      h("td", { class: "num" }, String(a.users ?? "—")),
      h("td", { class: "num" }, String(a.groups ?? "—")),
      h("td", { class: "num" }, a.high_risk_users ? h("span", { class: "pill crit" }, String(a.high_risk_users)) : "0"),
    ),
  );
  nodes.push(
    h(
      "section",
      { class: "group" },
      h("div", { class: "group-label" }, "云账号"),
      h(
        "div",
        { class: "card scroll" },
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, h("th", {}, "账号"), h("th", { class: "num" }, "用户"), h("th", { class: "num" }, "组"), h("th", { class: "num" }, "高危用户"))),
          h("tbody", {}, acctRows.length ? acctRows : h("tr", {}, h("td", { colspan: "4", class: "muted" }, "没有数据"))),
        ),
      ),
    ),
  );

  nodes.push(peopleSection(people));
  const editable = Boolean(records && records.enabled);
  if (records && records.error) nodes.push(h("div", { class: "banner warn" }, `名册审核暂不可用：${records.error}`));
  nodes.push(unlinkedSection(people.unlinked_accounts || [], editable ? people.assignable || [] : null));
  if (editable) nodes.push(recordsSection(records.records || []));
  return nodes;
}

const RECORD_KIND = { link: "人工确认给", rejected: "驳回对应", service: "服务号" };

function recordsSection(items) {
  const rows = items.map((r) =>
    h(
      "tr",
      {},
      h("td", {}, h("span", { class: "mono" }, r.account)),
      h("td", {}, h("span", { class: r.kind === "rejected" ? "pill warn" : "pill" }, RECORD_KIND[r.kind] || r.kind)),
      h("td", {}, r.name ? h("span", {}, r.name, " ") : null, r.email ? h("span", { class: "pmail" }, r.email) : h("span", { class: "muted" }, "—")),
      h(
        "td",
        {},
        h(
          "button",
          {
            type: "button",
            class: "btn small ghost",
            onclick: (e) => review({ op: "undo", account: r.account }, `撤销对 ${r.account} 的人工记录？会回到规则推断的结果。`, e.currentTarget, renderAdmin),
          },
          "撤销",
        ),
      ),
    ),
  );
  return h(
    "details",
    { class: "card fold" },
    h("summary", {}, "人工记录", h("span", { class: "muted" }, String(items.length))),
    h(
      "div",
      { class: "scroll" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, h("th", {}, "账号"), h("th", {}, "类型"), h("th", {}, "人员"), h("th", {}, ""))),
        h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: "4", class: "muted" }, "没有"))),
      ),
    ),
  );
}

function peopleSection(people) {
  const tbody = h("tbody");
  const count = h("span", { class: "muted" });

  const matches = (p, q) => {
    if (!q) return true;
    const hay = [p.name, p.email, ...(p.accounts || []).map((a) => a.name)].join(" ").toLowerCase();
    return hay.includes(q);
  };

  const fill = () => {
    clear(tbody);
    const q = state.peopleQuery.trim().toLowerCase();
    const rows = (people.people || []).filter((p) => matches(p, q));
    count.textContent = `${rows.length} 人`;
    if (!rows.length) {
      tbody.append(h("tr", {}, h("td", { colspan: "5", class: "muted" }, "没有符合条件的人")));
      return;
    }
    for (const p of rows) tbody.append(personRow(p));
  };

  const search = h("input", {
    class: "search",
    type: "search",
    placeholder: "搜索姓名、邮箱、用户名",
    "aria-label": "搜索人员",
    value: state.peopleQuery,
    oninput: (e) => {
      state.peopleQuery = e.target.value;
      fill();
    },
  });

  const chips = FILTERS.map(([key, label]) =>
    h(
      "button",
      {
        type: "button",
        class: key === state.peopleFilter ? "filter active" : "filter",
        "aria-pressed": key === state.peopleFilter ? "true" : "false",
        onclick: () => {
          if (state.peopleFilter === key) return;
          state.peopleFilter = key;
          renderAdmin();
        },
      },
      label,
    ),
  );

  fill();
  return h(
    "section",
    { class: "group" },
    h("div", { class: "group-label" }, "人员"),
    h("div", { class: "toolbar" }, ...chips, count, search),
    h(
      "div",
      { class: "card scroll" },
      h(
        "table",
        { class: "people" },
        h(
          "thead",
          {},
          h("tr", {}, h("th", {}, "姓名"), h("th", { class: "num" }, "账号数"), h("th", {}, "云账号"), h("th", {}, "高危"), h("th", {}, "union_id")),
        ),
        tbody,
      ),
    ),
  );
}

function personRow(p) {
  const dups = new Set(p.same_account_duplicates || []);
  const open = () => {
    location.hash = `person=${encodeURIComponent(p.key)}`;
  };
  const chips = (p.accounts || []).map((a) => {
    const dup = dups.has(`${a.platform}/${a.account}`);
    const tag = platformTag(a.platform, `${a.platform_display || a.platform} · ${a.name}`, [dup ? "dup" : "", a.in_snapshot === false ? "" : ""].join(" "));
    tag.title = [a.account_label || a.account, dup ? "同一云账号里有多个号" : "", a.in_snapshot === false ? "快照中不存在" : ""].filter(Boolean).join("；");
    if (a.in_snapshot === false) tag.style.opacity = "0.55";
    return tag;
  });
  for (const item of p.pending || []) {
    const tag = platformTag(item.platform, `${item.platform_display || item.platform} · ${item.name}`, "pending");
    tag.title = `${item.account_label || ""} 待确认对应（${PENDING_STATUS[item.status] || item.status || ""}）`;
    chips.push(tag);
  }
  const risk = p.high_risk || [];
  return h(
    "tr",
    {
      class: "prow",
      tabindex: "0",
      onclick: open,
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      },
    },
    h("td", { "data-label": "" }, h("span", { class: "pname" }, p.name || "未命名"), p.email ? h("span", { class: "pmail" }, p.email) : null),
    h("td", { class: "num", "data-label": "账号数" }, String(p.account_count ?? (p.accounts || []).length), p.pending_count ? h("span", { class: "muted", title: "待确认对应" }, ` +${p.pending_count}`) : null, dups.size ? h("span", { class: "pill warn", title: "同一云账号里有多个号" }, " 重复") : null),
    h("td", { "data-label": "" }, h("div", { class: "chips" }, chips.length ? chips : h("span", { class: "muted" }, "无"))),
    h("td", { "data-label": "高危" }, risk.length ? h("span", { class: "pill crit", title: risk.join("\n") }, String(risk.length)) : h("span", { class: "muted" }, "—")),
    h("td", { "data-label": "union_id" }, p.bound ? h("span", { class: "pill good" }, "已绑定") : h("span", { class: "pill warn" }, "未绑定")),
  );
}

function unlinkedSection(items, assignable) {
  const listId = "assignable-people";
  const actionCell = (a) => {
    if (!assignable || a.kind === "service") return null;
    const input = h("input", {
      class: "search assign",
      type: "email",
      list: listId,
      placeholder: "分配给（邮箱）",
      "aria-label": `把 ${a.name} 分配给`,
    });
    const key = accountKey(a);
    return h(
      "td",
      {},
      h(
        "div",
        { class: "review-actions" },
        input,
        h(
          "button",
          {
            type: "button",
            class: "btn small",
            onclick: (e) => {
              const email = input.value.trim().toLowerCase();
              if (!email) {
                input.focus();
                return;
              }
              const hit = assignable.find((p) => p.email === email);
              const who = hit ? `${hit.name}（${email}）` : email;
              review({ op: "assign", email, account: key }, `把 ${a.name} 分配给 ${who}？分配后会进入 IAM 属性表。`, e.currentTarget, renderAdmin);
            },
          },
          "分配",
        ),
        h(
          "button",
          {
            type: "button",
            class: "btn small ghost",
            onclick: (e) => review({ op: "service", account: key }, `把 ${a.name} 标记为服务号？服务号不会分配给任何人。`, e.currentTarget, renderAdmin),
          },
          "标为服务号",
        ),
      ),
    );
  };
  const rows = items.map((a) =>
    h(
      "tr",
      {},
      h("td", {}, platformTag(a.platform, a.platform_display), " ", h("span", { class: "muted" }, a.account_label || a.account || "")),
      h("td", {}, h("span", { class: "mono" }, a.name || ""), a.display_name ? h("span", { class: "pmail" }, a.display_name) : null),
      h("td", {}, a.kind === "service" ? h("span", { class: "pill" }, "服务号") : h("span", { class: "pill warn" }, "待确认")),
      h("td", { class: "num" }, String(a.policy_count ?? "—")),
      h("td", {}, (a.high_risk || []).length ? h("span", { class: "pill crit", title: a.high_risk.join("\n") }, String(a.high_risk.length)) : h("span", { class: "muted" }, "—")),
      assignable ? actionCell(a) || h("td", {}) : null,
    ),
  );
  const datalist = assignable
    ? h("datalist", { id: listId }, assignable.map((p) => h("option", { value: p.email }, p.name || p.email)))
    : null;
  return h(
    "details",
    { class: "card fold" },
    h("summary", {}, "未关联到人的云账号", h("span", { class: "muted" }, String(items.length))),
    h(
      "div",
      { class: "scroll" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, h("th", {}, "平台"), h("th", {}, "用户名"), h("th", {}, "类型"), h("th", { class: "num" }, "权限条数"), h("th", {}, "高危"), assignable ? h("th", {}, "处理") : null)),
        h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: assignable ? "6" : "5", class: "muted" }, "没有"))),
      ),
      datalist,
    ),
  );
}

// ── 启动 ─────────────────────────────────────────────────────────────────
async function boot() {
  try {
    state.session = await api("/api/session");
    state.loginUrl = state.session.login_url || state.loginUrl;
  } catch (err) {
    const view = errorView(err instanceof ApiError ? err : new ApiError(0, String(err)), boot);
    if (view) mount(view);
    return;
  }
  renderTopbar();
  route();
}

window.addEventListener("hashchange", () => {
  if (state.session && state.session.authenticated) route();
});

boot();
