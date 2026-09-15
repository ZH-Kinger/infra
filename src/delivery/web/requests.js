// 云账号申请：选模板 → 填表 → 飞书审批 → 开通 / 领取。管理员看全部申请、重试开通。
//
// 体验约定：
//   · 每一步都说清楚「接下来会发生什么」：提交前预览审批通过后开通的内容，提交后告诉员工去飞书看审批。
//   · 不能申请的模板照样展示，但置灰并写明原因，而不是让人找不到。
//   · 凭证和密码只在领取那一刻显示，离开页面就没了；页面上明确提示这一点。

import { ago, api, ApiError, apiPost, copyButton, fill, fmtTime, h, mount, openDrawer, PLATFORM_NAME, platformTag, requestTitle, safeHttps } from "./core.js";

const KIND_ORDER = ["permission", "credential", "account"];
const KIND_INFO = {
  permission: { title: "云账号权限", desc: "给你已有的子账号加上某项权限，比如 OSS 只读。" },
  credential: { title: "访问凭证", desc: "申请临时 AccessKey，给脚本和 CLI 用，到期自动失效。" },
  account: { title: "开账号", desc: "在还没有账号的云上开一个子账号。" },
};
const KIND_KEY_LABEL = { permission: "权限包", policy: "权限策略", credential: "访问凭证", account: "开账号" };
const RISK = { low: ["低风险", "good"], medium: ["中风险", "warn"], high: ["高风险", "crit"] };
const STATUS_TONE = {
  pending_approval: "accent",
  approved: "accent",
  executing: "accent",
  claimable: "good",
  done: "good",
  failed: "crit",
  submit_failed: "crit",
  rejected: "warn",
  withdrawn: "",
  expired: "",
  closed: "",
  submitting: "",
};

function statusPill(req) {
  return h("span", { class: `pill ${STATUS_TONE[req.status] || ""}` }, req.status_label || req.status);
}

function riskPill(risk) {
  const [label, tone] = RISK[risk] || RISK.low;
  return h("span", { class: `pill ${tone}` }, label);
}

// 云账号显示名（「阿里云主账号」这类），从 /api/me 取一次缓存在内存里；取不到就显示账号 ID
const ACCOUNT_LABELS = new Map();
let labelsLoaded = null;
function loadAccountLabels() {
  labelsLoaded ||= api("/api/me")
    .then((me) => {
      for (const a of (me && me.accounts) || []) if (a.account_label) ACCOUNT_LABELS.set(`${a.platform}/${a.account}`, a.account_label);
    })
    .catch(() => {
      labelsLoaded = null;
    });
  return labelsLoaded;
}

function accountLabel(tpl) {
  const label = tpl.account_label || ACCOUNT_LABELS.get(`${tpl.platform}/${tpl.account}`);
  return `${PLATFORM_NAME[tpl.platform] || tpl.platform} · ${label || tpl.account}`;
}

function pageHead(title, lede, ...extra) {
  return h("header", { class: "masthead" }, h("div", { class: "masthead-row" }, h("h1", {}, title), ...extra), lede ? h("p", { class: "lede" }, lede) : null);
}

// ── 申请页 ────────────────────────────────────────────────────────────────
// 布局：顶部按类型分三栏（权限 / 凭证 / 开账号，三种意图不混在一起）；
// 栏内可搜索、按分类 / 云 / 状态筛选；每一行直接写明「你有没有」。
// 点「申请」从右侧滑出表单，列表和筛选保持原样，关掉就回到刚才的位置。

const STATE_PILL = {
  owned: ["已拥有", "good"],
  pending: ["申请中", "accent"],
  ready: ["可领取", "good"],
  unavailable: ["不可申请", ""],
};
const STATE_FILTERS = [
  ["all", "全部"],
  ["available", "可申请"],
  ["owned", "已拥有"],
  ["pending", "申请中"],
];

export function requestRoutes(ctx) {
  const { load, errorView } = ctx;
  // 筛选条件跨次渲染保留：从申请详情返回时不用重新筛
  const filters = { kind: "", q: "", category: "all", platform: "all", state: "all" };

  function renderApply(selectedId) {
    return load(
      async () => {
        const [data, me] = await Promise.all([api("/api/requests/options"), api("/api/me").catch(() => null)]);
        return { data, me };
      },
      ({ data, me }) => {
        const labels = new Map(((me && me.accounts) || []).map((a) => [`${a.platform}/${a.account}`, a.account_label]));
        const options = (data.options || []).map((o) => ({ ...o, account_label: labels.get(`${o.platform}/${o.account}`) || "" }));
        mount(applyPage(options, data.my_accounts || []));
        const selected = options.find((o) => o.id === selectedId);
        if (selected && selected.available) openForm(selected, data.my_accounts || []);
      },
    );
  }

  function applyPage(options, myAccounts) {
    const head = pageHead("申请", "提交后会以你的名义发起飞书审批，审批通过后自动开通。", h("a", { class: "btn ghost small push", href: "#requests" }, "我的申请"));
    if (!options.length) {
      return [head, h("div", { class: "card empty" }, h("h2", {}, "还没有可申请的项目"), h("p", {}, "管理员还没有配置申请模板。需要开通云资源请先联系管理员。"))];
    }
    if (!options.some((o) => o.kind === filters.kind)) {
      // 第一次打开：默认停在有可申请项的那一栏（没有云账号的新同事直接看到「开账号」）
      filters.kind = KIND_ORDER.find((k) => options.some((o) => o.kind === k && o.available)) || KIND_ORDER.find((k) => options.some((o) => o.kind === k));
    }

    const kindTabs = h("div", { class: "segmented", role: "tablist", "aria-label": "申请类型" });
    const toolbar = h("div", { class: "apply-tools" });
    const summary = h("p", { class: "apply-summary muted", "aria-live": "polite" });
    const list = h("div", { class: "apply-list" });

    function renderTabs() {
      fill(kindTabs,
        ...KIND_ORDER.filter((k) => options.some((o) => o.kind === k)).map((k) => {
          const n = options.filter((o) => o.kind === k).length;
          const active = k === filters.kind;
          return h(
            "button",
            {
              type: "button",
              role: "tab",
              class: active ? "seg active" : "seg",
              "aria-selected": active ? "true" : "false",
              onclick: () => {
                if (filters.kind === k) return;
                Object.assign(filters, { kind: k, category: "all", state: "all" });
                renderAll();
              },
            },
            h("span", { class: "seg-title" }, KIND_INFO[k].title, h("span", { class: "seg-n" }, String(n))),
            h("span", { class: "seg-desc" }, KIND_INFO[k].desc),
          );
        }),
      );
    }

    const search = h("input", {
      id: "apply-search",
      class: "input search-input",
      type: "search",
      placeholder: "搜索名称、服务或用户组，如 OSS、PAI",
      autocomplete: "off",
      value: filters.q,
      "aria-label": "搜索可申请的项目",
    });
    search.addEventListener("input", () => {
      filters.q = search.value;
      renderList();
    });

    function chipRow(label, key, entries) {
      return h(
        "div",
        { class: "chip-row", role: "group", "aria-label": label },
        h("span", { class: "chip-label" }, label),
        entries.map(([value, text, n]) =>
          h(
            "button",
            {
              type: "button",
              class: filters[key] === value ? "chip active" : "chip",
              "aria-pressed": filters[key] === value ? "true" : "false",
              onclick: () => {
                filters[key] = value;
                renderTools();
                renderList();
              },
            },
            text,
            n === undefined ? null : h("span", { class: "chip-n" }, String(n)),
          ),
        ),
      );
    }

    function renderTools() {
      const ofKind = options.filter((o) => o.kind === filters.kind);
      const rows = [];
      const categories = [...new Set(ofKind.map((o) => o.category).filter(Boolean))];
      if (categories.length) {
        if (filters.category !== "all" && !categories.includes(filters.category)) filters.category = "all";
        rows.push(chipRow("分类", "category", [["all", "全部", ofKind.length], ...categories.map((c) => [c, c, ofKind.filter((o) => o.category === c).length])]));
      }
      const platforms = [...new Set(ofKind.map((o) => o.platform))];
      if (platforms.length > 1) rows.push(chipRow("云", "platform", [["all", "全部"], ...platforms.map((p) => [p, PLATFORM_NAME[p] || p])]));
      if (filters.kind !== "account") rows.push(chipRow("状态", "state", STATE_FILTERS.map(([v, t]) => [v, t, ofKind.filter((o) => matchState(o, v)).length])));
      fill(toolbar, h("div", { class: "search-wrap" }, search), ...rows);
    }

    function matchState(o, value) {
      if (value === "all") return true;
      if (value === "pending") return o.state === "pending" || o.state === "ready";
      if (value === "available") return o.state === "available";
      return o.state === value;
    }

    function matchQuery(o, q) {
      if (!q) return true;
      const hay = [o.title, o.description, o.category, o.id, PLATFORM_NAME[o.platform], o.account, o.account_label, ...(o.groups || [])].join(" ").toLowerCase();
      return q
        .toLowerCase()
        .split(/\s+/)
        .filter(Boolean)
        .every((word) => hay.includes(word));
    }

    function renderList() {
      const ofKind = options.filter((o) => o.kind === filters.kind);
      const shown = ofKind.filter((o) => (filters.category === "all" || o.category === filters.category) && (filters.platform === "all" || o.platform === filters.platform) && matchState(o, filters.state) && matchQuery(o, filters.q.trim()));
      const owned = ofKind.filter((o) => o.state === "owned").length;
      const pending = ofKind.filter((o) => o.state === "pending" || o.state === "ready").length;
      const facts = [`共 ${ofKind.length} 项`];
      if (filters.kind !== "account") facts.push(`你已拥有 ${owned} 项`);
      if (pending) facts.push(`${pending} 项在申请中`);
      if (shown.length !== ofKind.length) facts.push(`当前显示 ${shown.length} 项`);
      summary.textContent = facts.join(" · ");

      if (!shown.length) {
        const anyFilter = filters.q.trim() || filters.category !== "all" || filters.platform !== "all" || filters.state !== "all";
        fill(list,
          h(
            "div",
            { class: "card empty" },
            h("h2", {}, anyFilter ? "没有符合条件的项目" : "这一类暂时没有可申请的项目"),
            h("p", {}, "找不到需要的权限，可以联系管理员新增申请模板。"),
            anyFilter
              ? h(
                  "button",
                  {
                    type: "button",
                    class: "btn ghost small",
                    onclick: () => {
                      Object.assign(filters, { q: "", category: "all", platform: "all", state: "all" });
                      search.value = "";
                      renderTools();
                      renderList();
                    },
                  },
                  "清除筛选",
                )
              : null,
          ),
        );
        return;
      }
      // 有分类且没按分类筛时按分类分段，否则一张列表
      const groups = new Map();
      for (const o of shown) {
        const key = filters.category === "all" && o.category ? o.category : "";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(o);
      }
      const keys = [...groups.keys()].sort((a, b) => (a === "") - (b === ""));
      fill(list,
        ...keys.map((key) =>
          h(
            "section",
            { class: "group" },
            key || groups.size > 1 ? h("div", { class: "group-label" }, key || "其他") : null,
            h("div", { class: "card list" }, groups.get(key).map((o) => optionRow(o, myAccounts))),
          ),
        ),
      );
    }

    function renderAll() {
      renderTabs();
      renderTools();
      renderList();
    }
    renderAll();
    const more = h(
      "a",
      { class: "card hint-card", href: "#permissions" },
      h("div", {}, h("b", {}, "找不到需要的权限？"), h("span", { class: "muted" }, " 在「权限列表」里可以从云上全部权限策略中搜索、勾选申请。")),
      h("span", { class: "hint-go" }, "打开权限列表 →"),
    );
    const syncMore = () => (more.hidden = filters.kind !== "permission");
    kindTabs.addEventListener("click", () => setTimeout(syncMore, 0));
    syncMore();
    return [head, kindTabs, h("div", { class: "apply-panel" }, toolbar, summary), list, more];
  }

  function optionRow(o, myAccounts) {
    const [pillText, tone] = STATE_PILL[o.state] || [];
    let action = null;
    if (o.state === "pending" || o.state === "ready") {
      action = h("a", { class: "btn ghost small", href: `#request=${encodeURIComponent(o.request_id)}` }, o.state === "ready" ? "去领取" : "查看申请");
    } else if (o.available) {
      action = h("button", { type: "button", class: o.state === "owned" ? "btn ghost small" : "btn small", onclick: () => openForm(o, myAccounts) }, o.state === "owned" ? "续期" : "申请");
    }
    const stateLine = o.state === "owned" && o.expires_at ? `${o.state_note}，${fmtTime(o.expires_at)} 到期` : o.state_note;
    return h(
      "div",
      { class: `opt${o.state === "unavailable" ? " off" : ""}` },
      h(
        "div",
        { class: "opt-main" },
        h("div", { class: "opt-title" }, platformTag(o.platform), h("span", { class: "opt-name" }, o.title), o.risk !== "low" ? riskPill(o.risk) : null),
        o.description ? h("p", { class: "opt-desc" }, o.description) : null,
        h("div", { class: "opt-meta" }, optionMeta(o)),
      ),
      h("div", { class: "opt-side" }, pillText ? h("span", { class: `pill ${tone}`, title: stateLine }, pillText) : null, action, stateLine ? h("div", { class: "opt-note" }, stateLine) : null),
    );
  }

  function accountName(o) {
    return o.account_label ? `${PLATFORM_NAME[o.platform] || o.platform} · ${o.account_label}` : accountLabel(o);
  }

  function optionMeta(o) {
    const parts = [accountName(o)];
    if (o.kind === "permission") {
      parts.push(`用户组 ${o.groups.join("、")}`);
      parts.push(o.max_days ? `最长 ${o.max_days} 天` : "长期");
    } else if (o.kind === "credential") {
      parts.push(`每次最长 ${o.max_hours} 小时`, `批准后 ${o.valid_days} 天内可领`);
    } else {
      if (o.groups.length) parts.push(`默认加入 ${o.groups.join("、")}`);
      if (o.console_login) parts.push("可登录控制台");
    }
    return parts.join(" · ");
  }

  // ── 申请表单（右侧抽屉）────────────────────────────────────────────────
  function openForm(o, myAccounts) {
    history.replaceState(null, "", `#apply=${encodeURIComponent(o.id)}`);
    openDrawer("drawer-title", (close) => applyForm(o, myAccounts, close), () => {
      if (location.hash.startsWith("#apply=")) history.replaceState(null, "", "#apply");
    });
  }

  function applyForm(o, myAccounts, close) {
    const fields = [];
    const read = {};
    const checks = [];
    const preview = h("p", { class: "preview-text" });
    const error = h("p", { class: "form-error", role: "alert", hidden: true });

    if (o.kind === "permission") {
      const mine = myAccounts.filter((a) => a.platform === o.platform && a.account === o.account);
      const select = h("select", { id: "f-user", class: "input" }, mine.map((a) => h("option", { value: a.name }, a.name)));
      fields.push(field("f-user", "给哪个子账号开通", select, "只能选名册里确认属于你的子账号。"));
      read.cloud_user = () => select.value;
      if (o.max_days) {
        const days = h("input", { id: "f-days", class: "input", type: "number", inputmode: "numeric", min: "1", max: String(o.max_days), value: String(Math.min(30, o.max_days)) });
        const presets = h(
          "div",
          { class: "presets" },
          [7, 30, 90, 180]
            .filter((d) => d <= o.max_days)
            .map((d) =>
              h(
                "button",
                {
                  type: "button",
                  class: "chip",
                  onclick: () => {
                    days.value = String(d);
                    update();
                  },
                },
                `${d} 天`,
              ),
            ),
        );
        days.addEventListener("input", update);
        fields.push(field("f-days", "需要多少天", h("div", { class: "inline" }, days, presets), `最长 ${o.max_days} 天，到期自动收回。`));
        read.days = () => Number.parseInt(days.value, 10);
        checks.push(() => (Number.isInteger(read.days()) && read.days() >= 1 && read.days() <= o.max_days ? "" : [days, `天数要在 1 到 ${o.max_days} 之间。`]));
      }
    } else if (o.kind === "credential") {
      const hours = h("select", { id: "f-hours", class: "input" }, Array.from({ length: o.max_hours }, (_, i) => h("option", { value: String(i + 1) }, `${i + 1} 小时`)));
      hours.value = String(Math.min(o.max_hours, 1));
      hours.addEventListener("change", update);
      fields.push(field("f-hours", "每次领取的有效时长", hours, `审批通过后 ${o.valid_days} 天内可以随时领取，每份凭证在这个时长后失效。`));
      read.hours = () => Number.parseInt(hours.value, 10);
    } else {
      // 模板里的规则按 Python 语法写，浏览器不一定认得：认不得就只做基本检查，交给服务端把关
      let pattern = /^[a-z0-9][a-z0-9._-]{1,63}$/;
      try {
        pattern = new RegExp(o.username_pattern);
      } catch {
        /* 保留默认规则 */
      }
      const rule = "小写字母开头，只用小写字母、数字、点和横线。";
      const username = h("input", { id: "f-username", class: "input", autocomplete: "off", spellcheck: "false", placeholder: "例如 zhang.san" });
      username.addEventListener("input", () => {
        username.classList.remove("invalid");
        update();
      });
      fields.push(field("f-username", "子账号用户名", username, rule));
      read.username = () => username.value.trim();
      checks.push(() => (pattern.test(read.username()) ? "" : [username, read.username() ? `用户名不符合规则：${rule}` : "请填写子账号用户名。"]));
    }

    const reason = h("textarea", { id: "f-reason", class: "input", rows: "4", maxlength: "500", placeholder: "做什么项目、要访问哪些资源。审批人会看到这段话。" });
    const counter = h("span", {}, "至少 5 个字 · 0 / 500");
    reason.addEventListener("input", () => {
      reason.classList.remove("invalid");
      counter.textContent = `至少 5 个字 · ${reason.value.length} / 500`;
    });
    fields.push(field("f-reason", "申请理由", reason, counter));
    checks.push(() => (reason.value.trim().length >= 5 ? "" : [reason, "请写一下申请理由，至少 5 个字。"]));

    const submit = h("button", { type: "submit", class: "btn" }, "提交并发起飞书审批");

    function payload() {
      const out = {};
      for (const [k, fn] of Object.entries(read)) out[k] = fn();
      return out;
    }
    function update() {
      const p = payload();
      if (o.kind === "permission") preview.textContent = `子账号 ${p.cloud_user || "（未选）"} 加入用户组 ${o.groups.join("、")}${p.days ? `，${p.days} 天后自动收回` : ""}。${o.state === "owned" ? "你现在已有这项权限，这次申请用于续期。" : ""}`;
      else if (o.kind === "credential") preview.textContent = `${o.valid_days} 天内，你可以随时在「我的申请」里领取 ${p.hours} 小时有效的临时凭证。`;
      else preview.textContent = `在 ${accountName(o)} 新建子账号 ${p.username || "（未填）"}${o.groups.length ? `，加入 ${o.groups.join("、")}` : ""}${o.console_login ? "；你可以领取一次性初始密码登录控制台" : ""}。`;
    }
    update();

    const form = h(
      "form",
      {
        class: "form",
        novalidate: true,
        onsubmit: async (e) => {
          e.preventDefault();
          error.hidden = true;
          for (const check of checks) {
            const problem = check();
            if (problem) {
              const [el, message] = problem;
              el.classList.add("invalid");
              el.focus();
              error.textContent = message;
              error.hidden = false;
              return;
            }
          }
          submit.disabled = true;
          submit.textContent = "正在发起审批…";
          try {
            const res = await apiPost("/api/requests", { template_id: o.id, payload: payload(), reason: reason.value.trim() });
            close();
            location.hash = `request=${encodeURIComponent(res.request.id)}&new=1`;
          } catch (err) {
            if (err instanceof ApiError && err.status === 401) {
              close();
              return errorView(err);
            }
            error.textContent = err.message;
            error.hidden = false;
            submit.disabled = false;
            submit.textContent = "提交并发起飞书审批";
          }
        },
      },
      h(
        "div",
        { class: "drawer-head" },
        h("div", {}, h("div", { class: "eyebrow" }, KIND_INFO[o.kind].title), h("h2", { id: "drawer-title" }, o.title), h("div", { class: "drawer-tags" }, platformTag(o.platform), riskPill(o.risk), o.category ? h("span", { class: "pill" }, o.category) : null)),
        h("button", { type: "button", class: "icon-btn", "aria-label": "关闭", onclick: close }, "✕"),
      ),
      h(
        "div",
        { class: "drawer-body" },
        o.description ? h("p", { class: "muted drawer-desc" }, o.description) : null,
        h("div", { class: "drawer-meta" }, optionMeta(o)),
        h("div", { class: "form-body" }, fields),
        h("div", { class: "preview" }, h("div", { class: "preview-label" }, "审批通过后"), preview),
      ),
      h("div", { class: "drawer-foot" }, error, h("div", { class: "drawer-actions" }, h("button", { type: "button", class: "btn ghost", onclick: close }, "取消"), submit), h("p", { class: "muted drawer-tip" }, "审批人在飞书里处理，结果会同步到「我的申请」。")),
    );
    setTimeout(() => form.querySelector(".drawer-body select, .drawer-body input, .drawer-body textarea")?.focus(), 0);
    return form;
  }

  function field(id, label, control, hint) {
    return h("div", { class: "field" }, h("label", { for: id }, label), control, hint ? h("div", { class: "hint" }, hint) : null);
  }

  // ── 我的申请 ────────────────────────────────────────────────────────────
  function renderMine(filter = "open") {
    return load(
      async () => (await Promise.all([api("/api/requests"), loadAccountLabels()]))[0],
      (data) => mount(listPage(data.requests || [], { admin: false, filter })),
    );
  }

  function renderAdminList(filter = "open") {
    return load(
      () => api("/api/admin/requests"),
      (data) => mount(listPage(data.requests || [], { admin: true, filter })),
    );
  }

  function listPage(requests, { admin, filter }) {
    const tabs = [
      ["open", "进行中", (r) => r.open],
      ["attention", admin ? "需要处理" : "待我操作", (r) => (admin ? ["failed", "executing", "submitting"].includes(r.status) || needsLink(r) : Boolean(r.actions.credential || r.actions.password))],
      ["closed", "已结束", (r) => !r.open],
      ["all", "全部", () => true],
    ];
    const pick = (tabs.find(([key]) => key === filter) || tabs[3])[2];
    const base = admin ? "#admin/requests" : "#requests";
    const head = admin
      ? pageHead("申请与开通", "全部员工的申请。开通失败的可以在这里重试；审批本身在飞书里处理。")
      : pageHead("我的申请", "", h("a", { class: "btn small push", href: "#apply" }, "新的申请"));
    const failed = admin ? requests.filter((r) => r.status === "failed").length : 0;
    const nodes = [head];
    if (failed) nodes.push(h("div", { class: "banner crit" }, h("b", {}, `${failed} 张申请开通失败，`), "审批已通过但没开通成功，请在「需要处理」里查看原因后重试。"));
    const search = h("input", { id: "req-search", class: "search", type: "search", placeholder: admin ? "搜索申请人、权限、单号" : "搜索权限、单号", "aria-label": "搜索申请", autocomplete: "off" });
    nodes.push(
      h(
        "div",
        { class: "toolbar" },
        tabs.map(([key, label, fn]) => {
          const n = requests.filter(fn).length;
          if (key === "attention" && !n && filter !== key) return null;
          return h("a", { class: `${key === filter ? "filter active" : "filter"}${key === "attention" && n ? (admin ? " alert" : " notice") : ""}`, href: `${base}?${key}`, "aria-current": key === filter ? "page" : null }, `${label} ${n}`);
        }),
        search,
      ),
    );
    const listSlot = h("div", { class: "list-slot" });
    // 管理员单子多：按类型和云再筛一层
    const facets = { kind: "all", platform: "all" };
    const facetRow = h("div", { class: "chip-rows" });
    function renderFacets() {
      const base = requests.filter(pick);
      const kinds = [...new Set(base.map(kindKey))];
      const platforms = [...new Set(base.map((r) => r.template.platform))];
      const row = (label, key, entries) =>
        h(
          "div",
          { class: "chip-row", role: "group", "aria-label": label },
          h("span", { class: "chip-label" }, label),
          entries.map(([value, text]) =>
            h(
              "button",
              {
                type: "button",
                class: facets[key] === value ? "chip active" : "chip",
                "aria-pressed": facets[key] === value ? "true" : "false",
                onclick: () => {
                  facets[key] = value;
                  renderFacets();
                  renderRows();
                },
              },
              text,
              h("span", { class: "chip-n" }, String(value === "all" ? base.length : base.filter((r) => (key === "kind" ? kindKey(r) : r.template.platform) === value).length)),
            ),
          ),
        );
      fill(facetRow,
        kinds.length > 1 ? row("类型", "kind", [["all", "全部"], ...kinds.map((k) => [k, KIND_KEY_LABEL[k] || k])]) : null,
        platforms.length > 1 ? row("云", "platform", [["all", "全部"], ...platforms.map((p) => [p, PLATFORM_NAME[p] || p])]) : null,
      );
      facetRow.hidden = !facetRow.childElementCount;
    }
    function renderRows() {
      const q = search.value.trim().toLowerCase();
      const shown = requests.filter(pick).filter((r) => (facets.kind === "all" || kindKey(r) === facets.kind) && (facets.platform === "all" || r.template.platform === facets.platform)).filter((r) => !q || [r.id, requestTitle(r), r.summary, r.kind_label, r.status_label, r.applicant && r.applicant.name, r.applicant && r.applicant.email].join(" ").toLowerCase().includes(q));
      if (!shown.length) {
        fill(listSlot,
          h(
            "div",
            { class: "card empty" },
            h("h2", {}, q ? "没有符合条件的申请" : filter === "open" ? "没有进行中的申请" : "没有申请"),
            admin || q ? null : h("p", {}, "需要云账号、权限或访问凭证时，从「申请」开始。"),
            admin || q ? null : h("a", { class: "btn small", href: "#apply" }, "去申请"),
          ),
        );
        return;
      }
      fill(listSlot, h("div", { class: "card list" }, shown.map((r) => requestRow(r, admin))));
    }
    search.addEventListener("input", renderRows);
    if (admin) {
      renderFacets();
      nodes.push(facetRow);
    }
    renderRows();
    nodes.push(listSlot);
    return nodes;
  }

  // 新开的子账号没能自动对应到申请人（比如名册里查不到企业邮箱），要管理员在名册里补
  // 服务端对照名册算：管理员在名册里确认后自动消失（只在管理员视图里有这个字段）
  function needsLink(r) {
    return r.link_pending === true;
  }

  function kindKey(r) {
    return r.template.id === "policy" ? "policy" : r.kind;
  }

  function requestRow(r, admin) {
    const href = admin ? `#admin/request=${encodeURIComponent(r.id)}` : `#request=${encodeURIComponent(r.id)}`;
    const cta = admin ? (needsLink(r) ? h("span", { class: "pill warn" }, "待对应到名册") : null) : r.actions.password ? h("span", { class: "pill good" }, "可领取初始密码") : null;
    return h(
      "a",
      { class: "row", href },
      h("div", { class: "row-main" }, h("div", { class: "row-title" }, platformTag(r.template.platform), h("span", { class: "title-break" }, requestTitle(r)), h("span", { class: "muted" }, r.kind_label)), h("div", { class: "row-sub" }, admin ? h("b", { class: "row-who" }, r.applicant.name || r.applicant.email || "未知申请人") : null, admin ? " · " : null, r.summary)),
      h("div", { class: "row-side" }, statusPill(r), cta, h("span", { class: "muted", title: r.created_at }, ago(r.created_at))),
    );
  }

  // ── 申请详情 ────────────────────────────────────────────────────────────
  function renderDetail(id, { admin, fresh }) {
    const url = admin ? `/api/admin/requests/${encodeURIComponent(id)}` : `/api/requests/${encodeURIComponent(id)}`;
    return load(
      async () => (await Promise.all([api(url), loadAccountLabels()]))[0],
      (data) => mount(detailPage(data.request, { admin, fresh })),
    );
  }

  function steps(r) {
    const s = r.status;
    const approvedLike = ["approved", "executing", "done", "failed", "claimable", "expired", "closed"].includes(s);
    const last = r.kind === "credential" ? "可领取" : "开通";
    const list = [
      ["提交", "done"],
      ["飞书审批", s === "pending_approval" ? "current" : approvedLike ? "done" : ["rejected", "withdrawn", "submit_failed"].includes(s) ? "stopped" : "todo"],
      [last, s === "executing" ? "current" : ["done", "claimable"].includes(s) ? "done" : s === "failed" ? "stopped" : "todo"],
    ];
    return h(
      "ol",
      { class: "steps" },
      list.map(([label, st]) => h("li", { class: `step ${st}` }, h("span", { class: "dot", "aria-hidden": "true" }), h("span", {}, label))),
    );
  }

  function detailPage(r, { admin, fresh }) {
    const back = admin ? "#admin/requests" : "#requests";
    const nodes = [
      h("header", { class: "masthead" }, h("div", { class: "masthead-row" }, h("a", { class: "btn ghost small", href: back }, "← 返回"), h("h1", { class: "title-break" }, requestTitle(r)), statusPill(r)), h("p", { class: "lede" }, `${r.kind_label} · ${accountLabel(r.template)} · 申请单 ${r.id}`)),
    ];
    if (fresh && r.status === "pending_approval") nodes.push(h("div", { class: "banner good" }, h("b", {}, "已提交。"), " 飞书审批已经以你的名义发起，审批人会在飞书里收到通知。审批通过后这里会自动更新。"));
    if (r.status === "submit_failed") nodes.push(h("div", { class: "banner crit" }, h("b", {}, "没能发起飞书审批。"), " ", lastNote(r) || "请稍后重新提交，或联系管理员。"));
    if (r.status === "failed") nodes.push(h("div", { class: "banner crit" }, h("b", {}, "审批已通过，但开通失败。"), " ", admin ? lastNote(r) : "管理员会处理，处理好后这里会更新。"));
    if (r.status === "rejected") nodes.push(h("div", { class: "banner warn" }, "审批没有通过。可以在飞书里查看审批意见，调整后重新申请。"));
    if (admin && needsLink(r)) nodes.push(h("div", { class: "banner warn" }, h("b", {}, "新账号还没对应到申请人。"), " 请在「人员与名册」里把这个子账号确认给申请人，否则他之后申请权限时选不到这个账号。 ", h("a", { href: "#admin" }, "去人员与名册 →")));
    if (r.status === "done" && r.result) nodes.push(h("div", { class: "banner good" }, h("b", {}, "已开通。"), " ", r.result));

    const actions = h("div", { class: "actions" });
    const secret = h("div", { class: "secret-slot" });
    const link = feishuLink(r);
    if (link) actions.append(link);
    if (r.actions.credential) actions.append(credentialAction(r, secret));
    if (r.actions.password) actions.append(passwordAction(r, secret));
    if (r.actions.withdraw) actions.append(simpleAction("撤回申请", `/api/requests/${encodeURIComponent(r.id)}/withdraw`, "撤回后飞书里的审批也会撤销。确定撤回？", admin, true));
    if (r.actions.retry) actions.append(simpleAction("重试开通", `/api/admin/requests/${encodeURIComponent(r.id)}/retry`, "会先重新核对飞书审批，通过后再开通。确定重试？", admin));
    if (r.actions.recover) actions.append(simpleAction("标记为失败", `/api/admin/requests/${encodeURIComponent(r.id)}/recover`, "这张单子长时间没有进展。标记为失败后可以核对云上状态再重试。确定？", admin, true));
    if (r.actions.close) actions.append(simpleAction("关闭申请", `/api/admin/requests/${encodeURIComponent(r.id)}/close`, "关闭后不能再开通或领取。确定关闭？", admin, true));
    const hint = nextStep(r, admin);
    nodes.push(h("div", { class: "card status-card" }, steps(r), hint || actions.childElementCount ? h("div", { class: "status-foot" }, hint ? h("p", { class: "status-hint" }, hint) : null, actions.childElementCount ? actions : null) : null));
    nodes.push(secret);

    const facts = [
      ["申请内容", r.summary],
      r.template.policies && r.template.policies.length ? ["授予的权限", policyList(r.template.policies)] : null,
      ["申请理由", r.reason],
      admin ? ["申请人", `${r.applicant.name || ""} ${r.applicant.email || ""}`.trim()] : null,
      r.valid_until ? ["领取截止", fmtTime(r.valid_until)] : null,
      r.expires_at ? ["权限到期", `${fmtTime(r.expires_at)}（到期自动收回）`] : null,
      ["提交时间", fmtTime(r.created_at)],
      admin && r.approval && r.approval.instance_code ? ["飞书审批实例", r.approval.instance_code] : null,
    ].filter(Boolean);
    nodes.push(h("section", { class: "group" }, h("div", { class: "group-label" }, "详情"), h("dl", { class: "card facts" }, facts.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v || "—")]))));
    nodes.push(
      h(
        "section",
        { class: "group" },
        h("div", { class: "group-label" }, "进度记录"),
        h("ol", { class: "card timeline" }, [...r.events].reverse().map((e) => h("li", {}, h("div", { class: "tl-head" }, h("b", {}, e.label), h("span", { class: "muted" }, `${e.actor} · ${fmtTime(e.at)}`)), e.note && e.note !== e.label ? h("div", { class: "tl-note" }, e.note) : null))),
      ),
    );
    return nodes;
  }

  function feishuLink(r) {
    // 飞书 AppLink 手机端和电脑端是两条路径：窄屏或移动端 UA 用手机链接
    const mobile = window.matchMedia("(max-width: 640px)").matches || /Android|iPhone|iPad/i.test(navigator.userAgent);
    const url = safeHttps(mobile ? r.approval_url_mobile || r.approval_url : r.approval_url);
    return url ? h("a", { class: "btn ghost small", href: url, target: "_blank", rel: "noopener noreferrer" }, "在飞书中查看审批 ↗") : null;
  }

  // 状态卡片里的一句话：现在卡在哪、接下来会发生什么
  function nextStep(r, admin) {
    const who = admin ? "申请人" : "你";
    switch (r.status) {
      case "pending_approval":
        return "等审批人在飞书里处理。通过后自动开通，这里会同步更新。";
      case "approved":
      case "executing":
        return "审批已通过，正在开通。";
      case "claimable":
        return r.valid_until ? `已批准，${fmtTime(r.valid_until)} 前${who}可以随时领取临时凭证。` : "已批准，可以领取临时凭证。";
      case "done":
        if (r.actions.password) return `子账号已开通。${who}可以领取一次性初始密码，首次登录必须修改。`;
        return r.expires_at ? `已开通，${fmtTime(r.expires_at)} 到期后自动收回。需要继续用请在到期前重新申请。` : "已开通。";
      case "failed":
        return admin ? "开通失败。核对原因后可以重试，或关闭这张申请。" : "";
      case "withdrawn":
        return "申请已撤回。";
      case "expired":
        return "领取期已过，需要的话请重新申请。";
      case "revoked":
        return "权限已到期收回。";
      default:
        return "";
    }
  }

  function policyList(policies) {
    return h(
      "ul",
      { class: "policy-facts" },
      policies.map((p) => h("li", {}, h("code", {}, p.name), p.type === "Custom" ? h("span", { class: "pill" }, "自定义") : null, p.risk ? riskPill(p.risk) : null)),
    );
  }

  function lastNote(r) {
    const e = [...r.events].reverse().find((x) => x.note && ["execute_failed", "submit_failed"].includes(x.event));
    return e ? e.note : "";
  }

  function simpleAction(label, url, question, admin, ghost) {
    const btn = h("button", { type: "button", class: ghost ? "btn ghost small" : "btn small" }, label);
    btn.addEventListener("click", async () => {
      if (!window.confirm(question)) return;
      btn.disabled = true;
      try {
        await apiPost(url, {});
        ctx.route();
      } catch (err) {
        btn.disabled = false;
        window.alert(err.message);
      }
    });
    return btn;
  }

  function credentialAction(r, slot) {
    const btn = h("button", { type: "button", class: "btn small" }, `领取 ${r.payload.hours} 小时临时凭证`);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "正在签发…";
      try {
        const res = await apiPost(`/api/requests/${encodeURIComponent(r.id)}/credential`, {});
        showCredential(slot, res.credential);
      } catch (err) {
        window.alert(err.message);
      }
      btn.disabled = false;
      btn.textContent = `再领一份（${r.payload.hours} 小时）`;
    });
    return btn;
  }

  // 单引号包裹，值里的单引号转成 '\'' ——粘进终端不会被 shell 解释
  function shq(value) {
    return `'${String(value).replaceAll("'", "'\\''")}'`;
  }

  function showCredential(slot, c) {
    const names =
      c.platform === "volcano"
        ? ["VOLCENGINE_ACCESS_KEY", "VOLCENGINE_SECRET_KEY", "VOLCENGINE_SESSION_TOKEN"]
        : ["ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_SECURITY_TOKEN"];
    const env = [c.access_key_id, c.access_key_secret, c.security_token].map((v, i) => `export ${names[i]}=${shq(v)}`).join("\n");
    const rows = [
      ["AccessKey ID", c.access_key_id],
      ["AccessKey Secret", c.access_key_secret],
      ["Security Token", c.security_token],
    ];
    const panel = h(
      "div",
      { class: "card secret", role: "region", "aria-label": "临时凭证" },
      h("div", { class: "secret-head" }, h("div", {}, h("h2", {}, "临时凭证"), h("p", { class: "muted" }, `${fmtTime(c.expiration)} 失效。只显示这一次，离开页面就看不到了，需要时可以再领。`)), h("button", { type: "button", class: "linkbtn", onclick: () => panel.remove() }, "隐藏")),
      h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, h("code", { class: "secret-value" }, v), copyButton(() => v))])),
      h("div", { class: "snippet" }, h("div", { class: "snippet-head" }, h("span", {}, "在终端里用（官方 CLI 和 SDK 都认这几个环境变量）"), copyButton(() => env, "复制全部")), h("pre", {}, env)),
    );
    fill(slot, panel);
    // 5 分钟后自动收起，减少凭证在屏幕上停留的时间
    setTimeout(() => panel.remove(), 5 * 60 * 1000);
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function passwordAction(r, slot) {
    const btn = h("button", { type: "button", class: "btn small" }, "领取控制台初始密码");
    btn.addEventListener("click", async () => {
      if (!window.confirm("初始密码只能领取一次，领取后请马上登录并修改。现在领取？")) return;
      btn.disabled = true;
      try {
        const res = await apiPost(`/api/requests/${encodeURIComponent(r.id)}/password`, {});
        const l = res.login;
        const link = safeHttps(l.login_url);
        const panel = h(
            "div",
            { class: "card secret" },
            h("div", { class: "secret-head" }, h("div", {}, h("h2", {}, "控制台登录信息"), h("p", { class: "muted" }, "只显示这一次，5 分钟后自动收起。首次登录必须修改密码。"))),
            h(
              "dl",
              { class: "kv" },
              h("dt", {}, "用户名"),
              h("dd", {}, h("code", { class: "secret-value" }, l.username), copyButton(() => l.username)),
              h("dt", {}, "初始密码"),
              h("dd", {}, h("code", { class: "secret-value" }, l.password), copyButton(() => l.password)),
            ),
            link ? h("div", { class: "form-foot" }, h("a", { class: "btn small", href: link, target: "_blank", rel: "noopener noreferrer" }, "打开控制台登录页")) : null,
        );
        fill(slot, panel);
        // 和凭证一样 5 分钟后收起
        setTimeout(() => panel.remove(), 5 * 60 * 1000);
        btn.remove();
      } catch (err) {
        btn.disabled = false;
        window.alert(err.message);
      }
    });
    return btn;
  }

  return { renderApply, renderMine, renderAdminList, renderDetail };
}
