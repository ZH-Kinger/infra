// 云账号申请：选模板 → 填表 → 飞书审批 → 开通 / 领取。管理员看全部申请、重试开通。
//
// 体验约定：
//   · 每一步都说清楚「接下来会发生什么」：提交前预览审批通过后开通的内容，提交后告诉员工去飞书看审批。
//   · 不能申请的模板照样展示，但置灰并写明原因，而不是让人找不到。
//   · 初始密码只在领取那一刻显示，离开页面就没了；页面上明确提示这一点。
//   · **访问凭证不在这里显示**：审批通过后直接发到对应飞书审批的评论里。面板页面会被截图、
//     被转发，登录态也可能留在别人电脑上；审批实例只有申请人和审批人看得到。

import { ago, api, ApiError, apiPost, copyButton, fill, fmtTime, h, mount, openDrawer, PLATFORM_NAME, platformTag, requestTitle, safeHttps } from "./core.js";
import { directoryFields, transferFields } from "./storage.js";

const KIND_ORDER = ["permission", "credential", "storage", "transfer", "resource", "account"];
const KIND_INFO = {
  permission: { title: "云账号权限", desc: "给你已有的子账号加上某项权限，比如 OSS 只读。" },
  credential: { title: "访问凭证", desc: "申请一份数据访问密钥。审批通过后直接发到审批评论里，到期自动失效。" },
  storage: { title: "数据目录", desc: "在数据桶里开一个新批次的目录。按数据类型放，生命周期和权限跟着类型走。" },
  transfer: { title: "数据迁移", desc: "把一个目录搬到另一个地方。填两个路径，走哪条链路由系统判断。" },
  resource: { title: "资源开通", desc: "ECS、RDS 这类要单独开的资源。审批通过后由管理员按流程创建。" },
  account: { title: "开账号", desc: "在还没有账号的云上开一个子账号。" },
};
const KIND_KEY_LABEL = { permission: "权限包", policy: "权限策略", credential: "访问凭证", storage: "数据目录", transfer: "数据迁移", resource: "资源", account: "开账号" };
const CAP_LABEL = { list: "查看清单", download: "下载", write: "上传" };
const RISK = { low: ["低风险", "good"], medium: ["中风险", "warn"], high: ["高风险", "crit"] };
const STATUS_TONE = {
  pending_approval: "accent",
  approved: "accent",
  executing: "accent",
  fulfilling: "accent",
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
      if (value === "pending") return o.state === "pending";
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
      const pending = ofKind.filter((o) => o.state === "pending").length;
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

    // 这张提示卡只在「云账号权限」那一栏出现。它的显示/隐藏必须和列表在**同一次**渲染里
    // 完成 —— 早先是挂在 kindTabs 的 click 上 setTimeout 切的，于是切换分类时页面会先
    // 按新列表重排一次、下一个宏任务再因为这张卡的出现/消失重排第二次，中间那一帧就是
    // 用户看到的「闪一下」。所以它要先于 renderAll() 创建出来，并且由 renderAll() 同步切。
    const more = h(
      "a",
      { class: "card hint-card", href: "#permissions" },
      h("div", {}, h("b", {}, "找不到需要的权限？"), h("span", { class: "muted" }, " 在「权限列表」里可以从云上全部权限策略中搜索、勾选申请。")),
      h("span", { class: "hint-go" }, "打开权限列表 →"),
    );

    function renderAll() {
      renderTabs();
      renderTools();
      renderList();
      more.hidden = filters.kind !== "permission";
    }
    renderAll();
    return [head, kindTabs, h("div", { class: "apply-panel" }, toolbar, summary), list, more];
  }

  function optionRow(o, myAccounts) {
    const [pillText, tone] = STATE_PILL[o.state] || [];
    let action = null;
    if (o.state === "pending") {
      action = h("a", { class: "btn ghost small", href: `#request=${encodeURIComponent(o.request_id)}` }, "查看申请");
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

  function duration(hours) {
    if (hours >= 24 && hours % 24 === 0) {
      const days = hours / 24;
      return days % 365 === 0 ? `${days / 365} 年` : `${days} 天`;
    }
    return `${hours} 小时`;
  }

  function optionMeta(o) {
    const parts = [accountName(o)];
    if (o.kind === "permission") {
      parts.push(`用户组 ${o.groups.join("、")}`);
      parts.push(o.max_days ? `最长 ${o.max_days} 天` : "长期");
    } else if (o.kind === "credential") {
      parts.push(`权限 ${(o.cap_labels || []).join("、")}`, `${o.buckets.length} 个桶可选`, `最长 ${duration(o.max_hours)}`);
    } else if (o.kind === "storage") {
      parts.push(`${(o.buckets || []).length} 个桶可选`, "目录建好后你只读");
    } else if (o.kind === "transfer") {
      parts.push("填两个路径", "链路自动判断", "搬完做端到端校验");
    } else if (o.kind === "resource") {
      if (o.options.length) parts.push(o.options.map((a) => a.label).join(" / ") + " 可选");
      if (o.cost_centers.length) parts.push("需填成本归属");
      parts.push(o.max_days ? `最长 ${o.max_days} 天` : "长期", "由管理员按流程开通");
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
    //: storage / transfer 的预览文案由 storage.js 产出 —— 那边才知道路径怎么拼、走哪条链
    let describe = null;
    const preview = h("p", { class: "preview-text" });
    const error = h("p", { class: "form-error", role: "alert", hidden: true });

    if (o.kind === "permission") {
      const mine = myAccounts.filter((a) => a.platform === o.platform && a.account === o.account);
      const select = h("select", { id: "f-user", class: "input" }, mine.map((a) => h("option", { value: a.name }, a.name)));
      fields.push(field("f-user", "给哪个子账号开通", select, ""));
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
        fields.push(field("f-days", "需要多少天", h("div", { class: "inline" }, days, presets), `最长 ${o.max_days} 天`));
        read.days = () => Number.parseInt(days.value, 10);
        checks.push(() => (Number.isInteger(read.days()) && read.days() >= 1 && read.days() <= o.max_days ? "" : [days, `天数要在 1 到 ${o.max_days} 之间。`]));
      }
    } else if (o.kind === "credential") {
      const subject = h("input", { id: "f-subject", class: "input", maxlength: "40", autocomplete: "off", placeholder: "留空就是你自己" });
      subject.addEventListener("input", update);
      fields.push(field("f-subject", "谁用这份凭证", subject, "外部合作方写对方单位名"));
      read.subject = () => subject.value.trim();

      const bucket = h("select", { id: "f-bucket", class: "input" }, o.buckets.map((b) => h("option", { value: b.name }, `${b.name}（${b.region}）`)));
      bucket.addEventListener("change", update);
      fields.push(field("f-bucket", "哪个桶", bucket, ""));
      read.bucket = () => bucket.value;

      if (o.allow_prefix) {
        const prefix = h("input", { id: "f-prefix", class: "input", autocomplete: "off", spellcheck: "false", placeholder: "留空 = 整个桶，例如 datasets/2026/" });
        prefix.addEventListener("input", update);
        fields.push(field("f-prefix", "限定到哪个目录（可选）", prefix, "范围越小越好"));
        read.prefix = () => prefix.value.trim();
      }

      // 8760 个 <option> 是不能看的。数字 + 单位，再给几个常用档
      const amount = h("input", { id: "f-hours", class: "input", type: "number", inputmode: "numeric", min: "1", value: "7" });
      const unit = h("select", { class: "input", "aria-label": "时长单位" }, [h("option", { value: "24" }, "天"), h("option", { value: "1" }, "小时")]);
      const toHours = () => Math.max(1, Math.round(Number.parseInt(amount.value, 10) || 0)) * Number.parseInt(unit.value, 10);
      const presets = h(
        "div",
        { class: "presets" },
        [[8, "8 小时"], [24 * 7, "7 天"], [24 * 30, "30 天"], [24 * 90, "90 天"], [24 * 365, "1 年"]]
          .filter(([hrs]) => hrs <= o.max_hours)
          .map(([hrs, label]) =>
            h("button", { type: "button", class: "chip", onclick: () => {
              if (hrs % 24 === 0) { unit.value = "24"; amount.value = String(hrs / 24); } else { unit.value = "1"; amount.value = String(hrs); }
              update();
            } }, label),
          ),
      );
      amount.addEventListener("input", update);
      unit.addEventListener("change", update);
      fields.push(field("f-hours", "用多久", h("div", { class: "inline" }, amount, unit, presets), `最长 ${duration(o.max_hours)}`));
      read.hours = toHours;
      checks.push(() => (toHours() >= 1 && toHours() <= o.max_hours ? "" : [amount, `时长要在 1 小时到 ${duration(o.max_hours)} 之间。`]));
    } else if (o.kind === "storage" || o.kind === "transfer") {
      const part = (o.kind === "storage" ? directoryFields : transferFields)(o, { field, update: () => update() });
      fields.push(...part.fields);
      Object.assign(read, part.read);
      checks.push(...part.checks);
      describe = part.describe;
    } else if (o.kind === "resource") {
      // 能选的一律给下拉：自由填写的规格调不了云 API，审批人也判断不了批的是什么
      const picks = {};
      if (o.options.length) {
        const rows = {};
        // 某些轴只在别的轴选了特定项时才有意义（不要公网 IP 就不用问计费方式）
        const hiddenNow = (axis) => Object.entries(axis.hidden_when || {}).some(([k, vs]) => vs.includes((picks[k] || {}).value));
        const sync = () => { for (const a of o.options) rows[a.id].hidden = hiddenNow(a); update(); };
        const nums = {};
        const texts = {};
        for (const axis of o.options) {
          let el;
          let hint = "";
          if (axis.text) {
            // 枚举不出来的东西（项目名）才给文本框。它的值不进云参数，只进审批单和台账
            el = h("input", { id: `f-opt-${axis.id}`, class: "input", type: "text", maxlength: String(axis.text.max), autocomplete: "off", placeholder: axis.text.hint || "" });
            hint = axis.text.hint || `最多 ${axis.text.max} 个字`;
            el.addEventListener("input", () => { el.classList.remove("invalid"); sync(); });
            texts[axis.id] = el;
            checks.push(() => {
              if (hiddenNow(axis)) return "";
              const v = el.value.replace(/\s+/g, " ").trim();
              if (!v) return [el, `请填写「${axis.label}」。`];
              if (v.length > axis.text.max) return [el, `「${axis.label}」最多 ${axis.text.max} 个字。`];
              return "";
            });
          } else if (axis.number) {
            // 连续值给数字框，不给下拉：磁盘大小列成几档只是把「填多少」换成「挑一个最接近的」
            const n = axis.number;
            el = h("input", { id: `f-opt-${axis.id}`, class: "input", type: "number", inputmode: "numeric", min: String(n.omit_zero ? 0 : n.min), max: String(n.max), step: String(n.step), value: String(n.default) });
            hint = n.omit_zero ? `${n.min}–${n.max}${n.unit}，填 0 表示不要` : `${n.min}–${n.max}${n.unit}`;
            el.addEventListener("input", () => { el.classList.remove("invalid"); sync(); });
            nums[axis.id] = el;
            checks.push(() => {
              if (hiddenNow(axis)) return "";
              const v = Number.parseInt(el.value, 10);
              if (!Number.isInteger(v)) return [el, `「${axis.label}」要填整数。`];
              if (n.omit_zero && v === 0) return "";
              if (v < n.min || v > n.max) return [el, `「${axis.label}」要在 ${n.min}–${n.max} 之间。`];
              if (n.step > 1 && v % n.step) return [el, `「${axis.label}」要是 ${n.step} 的整数倍。`];
              return "";
            });
          } else {
            el = h("select", { id: `f-opt-${axis.id}`, class: "input" }, axis.choices.map((c) => h("option", { value: c.id }, c.label)));
            el.addEventListener("change", sync);
            picks[axis.id] = el;
          }
          rows[axis.id] = field(`f-opt-${axis.id}`, axis.label, el, hint);
          fields.push(rows[axis.id]);
        }
        queueMicrotask(sync);
        // 三种轴各回各的键。文本轴要是漏在 choices 里，服务端会拿空串去找选项、报「没选」
        read.choices = () => Object.fromEntries(o.options.filter((a) => !a.number && !a.text && !hiddenNow(a)).map((a) => [a.id, picks[a.id].value]));
        read.numbers = () => Object.fromEntries(o.options.filter((a) => a.number && !hiddenNow(a)).map((a) => [a.id, Number.parseInt(nums[a.id].value, 10)]));
        read.texts = () => Object.fromEntries(o.options.filter((a) => a.text && !hiddenNow(a)).map((a) => [a.id, texts[a.id].value.replace(/\s+/g, " ").trim()]));
      } else {
        const specEl = h("input", { id: "f-spec", class: "input", maxlength: "500", autocomplete: "off", placeholder: o.spec_hint || "写明规格、地域、用途" });
        specEl.addEventListener("input", () => { specEl.classList.remove("invalid"); update(); });
        fields.push(field("f-spec", "要什么规格", specEl, o.spec_hint || ""));
        read.spec = () => specEl.value.trim();
        checks.push(() => (specEl.value.trim().length >= 2 ? "" : [specEl, "请写明要开什么规格。"]));
      }

      if (o.cost_centers.length) {
        const ccList = [...o.cost_centers.map((c) => h("option", { value: c.id }, c.label))];
        if (o.cost_center_other) ccList.push(h("option", { value: "other" }, "其他（自己填）"));
        const cc = h("select", { id: "f-cost", class: "input" }, [h("option", { value: "" }, "请选择"), ...ccList]);
        // 清单里没有时能自己填：不给填的话人只会随便挑一个最像的，那比写清楚更糟
        const other = h("input", { id: "f-cost-other", class: "input", maxlength: "40", autocomplete: "off", placeholder: "项目名或团队名" });
        const otherRow = field("f-cost-other", "算在谁头上", other, "");
        otherRow.hidden = true;
        const sync = () => { otherRow.hidden = !(o.cost_center_other && cc.value === "other"); update(); };
        cc.addEventListener("change", sync);
        other.addEventListener("input", () => { other.classList.remove("invalid"); update(); });
        fields.push(field("f-cost", "成本归属", cc, ""), otherRow);
        read.cost_center = () => cc.value;
        read.cost_center_name = () => other.value.trim();
        checks.push(() => (cc.value ? "" : [cc, "请选择成本归属。"]));
        checks.push(() => (cc.value !== "other" || other.value.trim().length >= 2 ? "" : [other, "请写清楚算在谁头上。"]));
      }

      const detail = h("textarea", { id: "f-detail", class: "input", rows: "2", maxlength: "500", placeholder: "挂载、特殊要求…（可选）" });
      detail.addEventListener("input", update);
      fields.push(field("f-detail", "补充说明（可选）", detail, ""));
      read.detail = () => detail.value.trim();

      if (o.max_days) {
        // 选日期不是填天数：「用到几月几号」才是人真正在想的事
        const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
        const plus = (days) => { const d = new Date(); d.setDate(d.getDate() + days); return d; };
        const until = h("input", { id: "f-until", class: "input", type: "date", min: iso(plus(1)), max: iso(plus(o.max_days)), value: iso(plus(Math.min(90, o.max_days))) });
        const presets = h("div", { class: "presets" }, [[30, "1 个月"], [90, "3 个月"], [180, "半年"], [365, "1 年"]].filter(([d]) => d <= o.max_days).map(([d, label]) =>
          h("button", { type: "button", class: "chip", onclick: () => { until.value = iso(plus(d)); update(); } }, label)));
        const untilHint = h("span", {});
        const days = () => Math.round((new Date(until.value + "T00:00:00") - new Date(iso(new Date()) + "T00:00:00")) / 864e5);
        const showDays = () => {
          const n = days();
          untilHint.textContent = until.value && Number.isFinite(n)
            ? `${n} 天（最长 ${o.max_days} 天）` : `最长 ${o.max_days} 天`;
        };
        until.addEventListener("input", () => { until.classList.remove("invalid"); showDays(); update(); });
        showDays();
        fields.push(field("f-until", "用到哪天", h("div", { class: "inline" }, until, presets), untilHint));
        read.until = () => until.value;
        checks.push(() => (until.value && until.value >= iso(plus(1)) && until.value <= iso(plus(o.max_days)) ? "" : [until, `到期日要在明天到 ${o.max_days} 天之内。`]));
      }
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
      if (describe) preview.textContent = describe(p);
      else if (o.kind === "permission") preview.textContent = `子账号 ${p.cloud_user || "（未选）"} 加入用户组 ${o.groups.join("、")}${p.days ? `，${p.days} 天后自动收回` : ""}。${o.state === "owned" ? "你现在已有这项权限，这次申请用于续期。" : ""}`;
      else if (o.kind === "credential") {
        const who = p.subject || "你自己";
        const scope = p.prefix ? `${p.bucket}/${p.prefix}` : `${p.bucket} 整个桶`;
        const how = o.sts_available && p.hours <= o.sts_max_hours ? "临时凭证（到点自动失效，云上不留任何东西）" : "长期凭证（子账号 + 写死时间窗的策略，到期自动失效并清理）";
        preview.textContent = `审批通过后给「${who}」发一份 ${duration(p.hours)} 的${how}，范围 ${scope}，权限 ${(o.cap_labels || []).join("、")}。凭证会发到这张审批的评论里，不显示在面板上。`;
      } else if (o.kind === "resource") {
        const what = o.options.length
          ? o.options.map((a) => {
              if (a.number) {
                const v = (p.numbers || {})[a.id];
                if (v === undefined) return "";
                return a.number.omit_zero && v === 0 ? `${a.label} 不要` : `${a.label} ${v}${a.number.unit}`;
              }
              if (a.text) {
                const tv = (p.texts || {})[a.id];
                return tv ? `${a.label} ${tv}` : "";
              }
              const c = (p.choices || {})[a.id];
              return c ? `${a.label} ${(a.choices.find((x) => x.id === c) || {}).label || "？"}` : "";
            }).filter(Boolean).join("、")
          : p.spec || "（未填）";
        const cc = p.cost_center === "other" ? p.cost_center_name : (o.cost_centers.find((c) => c.id === p.cost_center) || {}).label;
        preview.textContent = `审批通过后在 ${accountName(o)} 开通 ${what}` + (cc ? `，成本归属 ${cc}` : "") + (p.until ? `，用到 ${p.until}` : "，长期") + "。";
      }
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
    // 测试用，页面不读它：「哪一种轴的值进了 payload 的哪个键」是最容易写错的地方
    // （三个 read.* 的过滤条件），而那一步在提交之前不产生任何可见痕迹
    form.readPayload = payload;
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
      ["attention", admin ? "需要处理" : "待我操作", (r) => (admin ? ["failed", "executing", "submitting", "fulfilling"].includes(r.status) || needsLink(r) : Boolean(r.actions.password))],
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
    const approvedLike = ["approved", "executing", "done", "failed", "fulfilling", "closed"].includes(s);
    const list = [
      ["提交", "done"],
      ["飞书审批", s === "pending_approval" ? "current" : approvedLike ? "done" : ["rejected", "withdrawn", "submit_failed"].includes(s) ? "stopped" : "todo"],
      [r.kind === "credential" ? "签发凭证" : "开通", ["executing", "fulfilling"].includes(s) ? "current" : s === "done" ? "done" : s === "failed" ? "stopped" : "todo"],
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
    if (r.status === "done" && r.result) nodes.push(h("div", { class: "banner good" }, h("b", {}, r.kind === "credential" ? "凭证已发放。" : "已开通。"), " ", r.result));
    if (r.status === "fulfilling") nodes.push(h("div", { class: "banner" }, h("b", {}, "审批已通过，等待开通。"), " 这类资源由管理员按 IaC 流程创建，面板不直接创建。开通后这里会更新。"));
    if (r.kind === "credential" && ["done", "revoked"].includes(r.status)) nodes.push(h("div", { class: "banner" }, h("b", {}, "查看凭证的地址在飞书审批的评论里。"), " 那个链接可以反复打开，每次打开都会记在下面的事件里。面板存的是密文，自己也解不开。"));

    const actions = h("div", { class: "actions" });
    const secret = h("div", { class: "secret-slot" });
    const link = feishuLink(r);
    if (link) actions.append(link);
    if (r.actions.password) actions.append(passwordAction(r, secret));
    if (r.actions.fulfil) actions.append(fulfilAction(r));
    if (r.actions.withdraw) actions.append(simpleAction("撤回申请", `/api/requests/${encodeURIComponent(r.id)}/withdraw`, "撤回后飞书里的审批也会撤销。确定撤回？", admin, true));
    if (r.actions.retry) actions.append(simpleAction("重试开通", `/api/admin/requests/${encodeURIComponent(r.id)}/retry`, "会先重新核对飞书审批，通过后再开通。确定重试？", admin));
    // 号建出来了但登录名没写进公司 IAM —— 单子是「已完成」、没有重试按钮，而那个人登不进去。
    // 不给这个入口的话，唯一的补救是全量 iam-push，而那条路会连带触发删除闸门
    if (r.actions.push_iam) actions.append(simpleAction("补写登录名到公司 IAM", `/api/admin/requests/${encodeURIComponent(r.id)}/push_iam`, "子账号已建好，但登录名没写进公司 IAM —— 他现在登不进去。只补这一步，不碰云上账号。确定补写？", admin));
    if (r.actions.recover) actions.append(simpleAction("标记为失败", `/api/admin/requests/${encodeURIComponent(r.id)}/recover`, "这张单子长时间没有进展。标记为失败后可以核对云上状态再重试。确定？", admin, true));
    //: 在途的迁移单关掉之后云上会怎样，**要分情况说**（审计 R4 / 三审）：
    //:   · 跨云（oss ↔ tos）：源端那把钥匙下一轮会被撤掉，对方云上的任务接着跑、然后全部 403
    //:   · 同云迁移、CPFS/vePFS 预热沉降：没有钥匙要撤，云上任务会**照常跑完** ——
    //:     关单只是面板不再跟进。说成「会失败停下」的话，人会以为关单就能止损
    const scheme = (uri) => String(uri || "").split("://")[0];
    const pair = `${scheme(r.payload?.source)}->${scheme(r.payload?.dest)}`;
    const crossCloud = pair === "oss->tos" || pair === "tos->oss";
    const closeTip = r.kind !== "transfer" || r.move_stage !== "running"
      ? "关闭后这张单子不会再开通。确定关闭？"
      : crossCloud
        ? "这张跨云迁移正在搬。关闭后面板不再跟进；源端钥匙下一轮会被撤掉，云上那个任务会因此失败停下（控制台里会留一条失败记录）。确定关闭？"
        : "这张迁移正在搬。关闭后面板不再跟进，但**云上的任务会照常跑完** —— 要真停下得去控制台手动停。确定关闭？";
    if (r.actions.close) actions.append(simpleAction("关闭申请", `/api/admin/requests/${encodeURIComponent(r.id)}/close`, closeTip, admin, true));
    if (r.actions.reopen) actions.append(simpleAction("重新打开", `/api/admin/requests/${encodeURIComponent(r.id)}/reopen`, "放回关闭前的状态接着处理。原来那张飞书审批继续有效，不用重新审批。确定重新打开？", admin));
    if (r.actions.revoke) actions.append(simpleAction("作废凭证", `/api/${admin ? "admin/" : ""}requests/${encodeURIComponent(r.id)}/revoke`, "查看地址立刻失效，云上的子账号、密钥和策略一并删除。使用方要重新申请。确定作废？", admin, true));
    const hint = nextStep(r, admin);
    nodes.push(h("div", { class: "card status-card" }, steps(r), hint || actions.childElementCount ? h("div", { class: "status-foot" }, hint ? h("p", { class: "status-hint" }, hint) : null, actions.childElementCount ? actions : null) : null));
    nodes.push(secret);

    const facts = [
      ["申请内容", r.summary],
      r.template.policies && r.template.policies.length ? ["授予的权限", policyList(r.template.policies)] : null,
      ["申请理由", r.reason],
      admin ? ["申请人", `${r.applicant.name || ""} ${r.applicant.email || ""}`.trim()] : null,
      r.expires_at ? [r.kind === "credential" ? "凭证到期" : r.kind === "resource" ? "使用到期" : "权限到期", `${fmtTime(r.expires_at)}（${r.kind === "resource" ? "到期只提醒，不会自动删资源" : "到期自动收回"}）`] : null,
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
        return r.kind === "credential" ? "审批已通过，正在发放凭证。" : "审批已通过，正在开通。";
      case "fulfilling":
        return admin ? "审批已通过。按 IaC 流程创建好之后，点「登记开通结果」把实例信息填进台账。" : "审批已通过，等管理员开通。开通后这里会更新。";
      case "done":
        if (r.actions.push_iam) return `子账号已建好，但登录名没写进公司 IAM —— **${who}现在登不进去**。管理员点「补写登录名到公司 IAM」即可。`;
        if (r.actions.password) return `子账号已开通。${who}可以领取一次性初始密码，首次登录必须修改。`;
        if (r.kind === "credential") return r.expires_at ? `凭证已签发，查看地址在飞书审批的评论里，${fmtTime(r.expires_at)} 到期。` : "凭证已签发，查看地址在飞书审批的评论里。";
        return r.expires_at ? `已开通，${fmtTime(r.expires_at)} 到期后自动收回。需要继续用请在到期前重新申请。` : "已开通。";
      case "failed":
        return admin ? "开通失败。核对原因后可以重试，或关闭这张申请。" : "";
      case "withdrawn":
        return "申请已撤回。";
      case "closed":
        // 「原审批继续有效」是这里最要紧的一句：不说的话，管理员的默认反应是让人重新申请、
        // 重新找人批一遍，而那张批条其实一直还在，每次开通都会重新核对
        // 不能写成「原审批仍然有效」：被「只有申请人自己批」关掉的单子也走这里，
        // 对它那句话恰好是反的。重试时会重新核对，核不过就还是开不了 —— 照这个说
        if (r.actions.reopen) return "这张申请已关闭。原来那张飞书审批还在，「重新打开」后重试开通时会重新核对它，不用重新申请。";
        return admin ? "这张申请已关闭。云上可能还留着子账号，要收回请用「作废凭证」；清干净后才能重新打开。" : "这张申请已关闭。需要的话可以重新提交一张。";
      case "revoked":
        return r.kind === "credential" ? "凭证已到期失效，云上的子账号和密钥已清理。" : "权限已到期收回。";
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

  function fulfilAction(r) {
    const btn = h("button", { type: "button", class: "btn small" }, "登记开通结果");
    btn.addEventListener("click", () => {
      openDrawer("fulfil-title", (close) => {
        // 实例 ID 单独一栏，不要埋在描述里：**开通这一刻是唯一确定「这台机器是谁的」
        // 的时机**，填了这一栏，资产页的归属当场就指给申请人；埋在句子里就只能事后靠猜
        const ids = h("textarea", { id: "f-ids", class: "input", rows: "3", placeholder: "i-bp1xxxxxxxx\ni-bp1yyyyyyyy" });
        const note = h("input", { id: "f-note", class: "input", type: "text", placeholder: "例：ECS 通用 2 核 8G，杭州可用区 B" });
        const msg = h("p", { class: "muted" });
        const save = h("button", { class: "btn", type: "button" }, "登记");
        save.addEventListener("click", async () => {
          if (!note.value.trim()) { msg.textContent = "写一句开通了什么，这行会进台账。"; return; }
          save.disabled = true;
          msg.textContent = "登记中…";
          try {
            await apiPost(`/api/admin/requests/${encodeURIComponent(r.id)}/fulfil`, {
              note: note.value.trim(),
              resource_ids: ids.value.split(/[\s,，、;；]+/).filter(Boolean),
            });
            close();
            ctx.route();
          } catch (err) { msg.textContent = err.message; save.disabled = false; }
        });
        return h("div", { class: "drawer-card" },
          h("h2", { id: "fulfil-title" }, "登记开通结果"),
          h("div", { class: "drawer-body" },
            field("f-ids", "实例 ID", ids, "一行一个，或用逗号分隔。填了就会把这些资源在资产页指给申请人。"),
            field("f-note", "开通了什么", note, "规格、地域、数量——这行会进台账，申请人看得到。")),
          h("div", { class: "drawer-foot" }, h("div", { class: "actions" }, save,
            h("button", { class: "btn ghost", type: "button", onclick: close }, "取消")), msg));
      });
    });
    return btn;
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

  // applyForm / applyPage 页面本身用不到（走 openForm → openDrawer / renderApply → mount），
  // 导出它们是为了能被测试直接调用：申请页是「后端加了一种轴、前端不认识」和
  // 「切换分类时页面闪一下」这两类 bug 的藏身处，而它们都藏在 load() 和 openDrawer 后面，
  // 从 renderApply 那头点进来要连带 stub 掉整个请求层和对话框
  return { renderApply, renderMine, renderAdminList, renderDetail, applyForm, applyPage };
}
