// 权限列表：云账号上能申请的全部权限策略（阿里云 RAM / 火山 IAM 的系统策略和自定义策略）。
//
// 体验约定：
//   · 先选云账号（只列出你有子账号的），再搜索、筛选；每一行直接写明「你有没有」。
//   · 勾选多条一起申请，底部操作条始终显示已选数量；申请表单从右侧滑出，不离开列表。
//   · 策略几千条：一次只渲染一页，筛选在内存里做，输入不卡。

import { ApiError, PLATFORM_NAME, api, apiPost, fmtTime, h, mount, openDrawer, platformTag } from "./core.js";

const PAGE = 60;
const RISK = { low: ["低风险", "good"], medium: ["中风险", "warn"], high: ["高风险", "crit"] };
const STATE_PILL = { owned: ["已拥有", "good"], pending: ["申请中", "accent"], unavailable: ["不开放", ""] };
const STATES = [
  ["all", "全部"],
  ["available", "可申请"],
  ["owned", "已拥有"],
  ["pending", "申请中"],
  ["unavailable", "不开放"],
];
const RISKS = [
  ["all", "全部"],
  ["low", "低"],
  ["medium", "中"],
  ["high", "高"],
];

export function permissionRoutes(ctx) {
  const { load, errorView } = ctx;
  // 跨次渲染保留：从申请详情返回时还在原来的账号、原来的筛选
  const view = { account: "", q: "", state: "all", risk: "all", type: "all", service: "all", selected: new Map() };

  function renderPermissions() {
    return load(
      () => api("/api/policies"),
      (data) => mount(page(data)),
    );
  }

  function keyOf(acc) {
    return `${acc.platform}/${acc.account}`;
  }

  function page(data) {
    const accounts = data.accounts || [];
    const max = data.max_per_request || 10;
    const head = h(
      "header",
      { class: "masthead" },
      h("div", { class: "masthead-row" }, h("h1", {}, "权限列表"), h("a", { class: "btn ghost small push", href: "#apply" }, "常用权限包")),
      h("p", { class: "lede" }, "云账号上可以申请的全部权限。勾选需要的，提交后发起飞书审批，通过后自动授权给你的子账号，到期自动收回。"),
      data.captured_at ? h("p", { class: "lede" }, `列表更新于 ${fmtTime(data.captured_at)}`) : null,
    );
    if (!accounts.length) {
      return [
        head,
        h(
          "div",
          { class: "card empty" },
          h("h2", {}, "你还没有云账号"),
          h("p", {}, "权限要授给你自己的子账号。先申请开一个子账号，开通后这里就会列出能申请的权限。"),
          h("a", { class: "btn small", href: "#apply" }, "去申请开账号"),
        ),
      ];
    }
    if (!accounts.some((a) => keyOf(a) === view.account)) {
      view.account = keyOf(accounts[0]);
      view.selected.clear();
    }

    const accountBar = h("div", { class: "segmented", role: "tablist", "aria-label": "云账号" });
    const tools = h("div", { class: "apply-tools" });
    const summary = h("p", { class: "apply-summary muted", "aria-live": "polite" });
    const staleNote = h("div", { class: "banner warn", hidden: true }, "这个云账号的权限列表最近一次没更新成功，下面是之前的列表，可能缺少新加的策略。可以照常申请，开通前会再核对。");
    const list = h("div", { class: "apply-list" });
    const bar = h("div", { class: "select-bar", role: "region", "aria-label": "已选权限", hidden: true });
    let shown = PAGE;

    const current = () => accounts.find((a) => keyOf(a) === view.account);

    function renderAccounts() {
      accountBar.replaceChildren(
        ...accounts.map((acc) => {
          const active = keyOf(acc) === view.account;
          const owned = (acc.policies || []).filter((p) => p.state === "owned").length;
          return h(
            "button",
            {
              type: "button",
              role: "tab",
              class: active ? "seg active" : "seg",
              "aria-selected": active ? "true" : "false",
              onclick: () => {
                if (active) return;
                if (view.selected.size && !window.confirm("切换云账号会清空已选的权限，继续？")) return;
                view.account = keyOf(acc);
                view.selected.clear();
                Object.assign(view, { service: "all" });
                shown = PAGE;
                renderAll();
              },
            },
            h("span", { class: "seg-title" }, platformTag(acc.platform), acc.account_label || acc.account),
            h("span", { class: "seg-desc" }, `子账号 ${acc.cloud_user} · 共 ${acc.total ?? (acc.policies || []).length} 项 · 已拥有 ${owned} 项`),
          );
        }),
      );
      accountBar.hidden = accounts.length < 2;
    }

    const search = h("input", {
      id: "perm-search",
      class: "input search-input",
      type: "search",
      placeholder: "搜索策略名、说明或服务，如 OSS 只读、ECS、TOS",
      autocomplete: "off",
      value: view.q,
      "aria-label": "搜索权限",
    });
    search.addEventListener("input", () => {
      view.q = search.value;
      shown = PAGE;
      renderList();
    });

    function chips(label, key, entries) {
      return h(
        "div",
        { class: "chip-row", role: "group", "aria-label": label },
        h("span", { class: "chip-label" }, label),
        entries.map(([value, text, n]) =>
          h(
            "button",
            {
              type: "button",
              class: view[key] === value ? "chip active" : "chip",
              "aria-pressed": view[key] === value ? "true" : "false",
              onclick: () => {
                view[key] = value;
                shown = PAGE;
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
      const acc = current();
      const all = acc.policies || [];
      const services = new Map();
      for (const p of all) services.set(p.service || "其他", (services.get(p.service || "其他") || 0) + 1);
      if (view.service !== "all" && !services.has(view.service)) view.service = "all";
      const serviceSelect = h(
        "select",
        { id: "perm-service", class: "input compact", "aria-label": "按服务筛选" },
        h("option", { value: "all" }, `全部服务（${all.length}）`),
        [...services.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name, n]) => h("option", { value: name }, `${name}（${n}）`)),
      );
      serviceSelect.value = view.service;
      serviceSelect.addEventListener("change", () => {
        view.service = serviceSelect.value;
        shown = PAGE;
        renderList();
      });
      const hasCustom = all.some((p) => p.type === "Custom");
      tools.replaceChildren(
        h("div", { class: "search-row" }, search, serviceSelect),
        chips(
          "状态",
          "state",
          STATES.map(([v, t]) => [v, t, all.filter((p) => v === "all" || p.state === v).length]),
        ),
        h(
          "div",
          { class: "chip-rows" },
          chips("风险", "risk", RISKS.map(([v, t]) => [v, t])),
          hasCustom
            ? chips("类型", "type", [
                ["all", "全部"],
                ["System", "系统策略"],
                ["Custom", "自定义"],
              ])
            : null,
        ),
      );
    }

    function matches(p) {
      if (view.state !== "all" && p.state !== view.state) return false;
      if (view.risk !== "all" && p.risk !== view.risk) return false;
      if (view.type !== "all" && p.type !== view.type) return false;
      if (view.service !== "all" && (p.service || "其他") !== view.service) return false;
      const q = view.q.trim().toLowerCase();
      if (!q) return true;
      const hay = `${p.name} ${p.description} ${p.service}`.toLowerCase();
      return q.split(/\s+/).every((w) => hay.includes(w));
    }

    function renderList() {
      const acc = current();
      if (acc.error) {
        summary.textContent = "";
        list.replaceChildren(h("div", { class: "card empty" }, h("h2", {}, "这个云账号的权限列表暂时不可用"), h("p", {}, acc.error)));
        renderBar();
        return;
      }
      const all = acc.policies || [];
      const hits = all.filter(matches);
      staleNote.hidden = !acc.stale;
      const owned = all.filter((p) => p.state === "owned").length;
      const parts = [`共 ${all.length} 项`, `你已拥有 ${owned} 项`];
      if (hits.length !== all.length) parts.push(`符合条件 ${hits.length} 项`);
      summary.textContent = parts.join(" · ");
      if (!hits.length) {
        list.replaceChildren(
          h(
            "div",
            { class: "card empty" },
            h("h2", {}, "没有符合条件的权限"),
            h("p", {}, "换个关键词试试，比如服务的英文缩写（OSS、ECS、TOS）。"),
            h(
              "button",
              {
                type: "button",
                class: "btn ghost small",
                onclick: () => {
                  Object.assign(view, { q: "", state: "all", risk: "all", type: "all", service: "all" });
                  search.value = "";
                  renderTools();
                  renderList();
                },
              },
              "清除筛选",
            ),
          ),
        );
        renderBar();
        return;
      }
      const rows = hits.slice(0, shown).map((p) => policyRow(acc, p));
      const more = hits.length > shown ? h("button", { type: "button", class: "btn ghost more", onclick: () => ((shown += PAGE), renderList()) }, `显示更多（还有 ${hits.length - shown} 项）`) : null;
      list.replaceChildren(h("div", { class: "card list" }, rows), more);
      renderBar();
    }

    function selKey(p) {
      return `${p.type}:${p.name}`;
    }

    function policyRow(acc, p) {
      // 有到期时间的已拥有权限（通过申请开通的）可以勾选续期
      const renew = p.state === "owned" && Boolean(p.expires_at);
      const selectable = p.state === "available" || renew;
      const checked = view.selected.has(selKey(p));
      const box = h("input", {
        type: "checkbox",
        class: "check",
        id: `pol-${selKey(p)}`,
        "aria-label": `选择 ${p.name}`,
        disabled: !selectable,
      });
      box.checked = checked;
      box.addEventListener("change", () => {
        if (box.checked) {
          if (view.selected.size >= max) {
            box.checked = false;
            window.alert(`一次最多申请 ${max} 项，先提交这一批。`);
            return;
          }
          view.selected.set(selKey(p), p);
        } else {
          view.selected.delete(selKey(p));
        }
        row.classList.toggle("picked", box.checked);
        renderBar();
      });
      const [pill, tone] = STATE_PILL[p.state] || [];
      const [riskText, riskTone] = RISK[p.risk] || RISK.medium;
      let side = null;
      if (p.request_id && (p.state === "pending" || renew)) side = h("a", { class: "linkbtn", href: `#request=${encodeURIComponent(p.request_id)}`, onclick: (e) => e.stopPropagation() }, "查看申请");
      const note = p.state === "owned" && p.expires_at ? `${p.state_note}，${fmtTime(p.expires_at)} 到期` : p.state_note;
      const row = h(
        selectable ? "label" : "div",
        { class: `perm${checked ? " picked" : ""}${p.state === "unavailable" ? " off" : ""}`, for: selectable ? box.id : null },
        h("div", { class: "perm-check" }, selectable ? box : h("span", { class: `perm-glyph ${p.state}`, "aria-hidden": "true" }, { owned: "✓", pending: "…", unavailable: "–" }[p.state] || "")),
        h(
          "div",
          { class: "opt-main" },
          h("div", { class: "opt-title" }, h("span", { class: "perm-name" }, p.name), p.service ? h("span", { class: "pill" }, p.service) : null, h("span", { class: `pill ${riskTone}` }, riskText), p.type === "Custom" ? h("span", { class: "pill" }, "自定义") : null),
          p.description ? h("p", { class: "opt-desc" }, p.description) : null,
        ),
        h("div", { class: "perm-side" }, h("div", { class: "perm-side-row" }, pill ? h("span", { class: `pill ${tone}` }, pill) : null, side), note ? h("div", { class: "opt-note" }, note) : null),
      );
      return row;
    }

    function renderBar() {
      const n = view.selected.size;
      bar.hidden = n === 0;
      if (!n) return;
      const names = [...view.selected.values()].map((p) => p.name);
      bar.replaceChildren(
        h("div", { class: "select-bar-inner" }, h("div", { class: "select-count" }, h("b", {}, `已选 ${n} 项`), h("span", { class: "muted" }, names.slice(0, 3).join("、") + (n > 3 ? ` 等` : ""))), h("div", { class: "select-actions" }, h("button", { type: "button", class: "btn ghost small", onclick: () => (view.selected.clear(), renderList()) }, "清空"), h("button", { type: "button", class: "btn small", onclick: () => openRequest(current(), [...view.selected.values()], max) }, "申请选中的权限"))),
      );
    }

    function renderAll() {
      renderAccounts();
      renderTools();
      renderList();
    }
    renderAll();
    return [head, accountBar, staleNote, h("div", { class: "apply-panel" }, tools, summary), list, bar];
  }

  // ── 申请表单 ────────────────────────────────────────────────────────────
  function openRequest(acc, picked, max) {
    const maxDays = Math.min(...picked.map((p) => p.max_days || 30));
    const highest = picked.some((p) => p.risk === "high") ? "high" : picked.some((p) => p.risk === "medium") ? "medium" : "low";
    const error = h("p", { class: "form-error", role: "alert", hidden: true });
    const days = h("input", { id: "g-days", class: "input", type: "number", inputmode: "numeric", min: "1", max: String(maxDays), value: String(Math.min(30, maxDays)) });
    const reason = h("textarea", { id: "g-reason", class: "input", rows: "4", maxlength: "500", placeholder: "做什么项目、要访问哪些资源。审批人会看到这段话。" });
    const counter = h("span", {}, "至少 5 个字 · 0 / 500");
    const preview = h("p", { class: "preview-text" });
    const submit = h("button", { type: "submit", class: "btn" }, "提交并发起飞书审批");
    const update = () => {
      const d = Number.parseInt(days.value, 10);
      preview.textContent = `子账号 ${acc.cloud_user} 获得上面这 ${picked.length} 项权限${Number.isInteger(d) && d > 0 ? `，${d} 天后自动收回` : ""}。`;
    };
    days.addEventListener("input", () => (days.classList.remove("invalid"), update()));
    reason.addEventListener("input", () => {
      reason.classList.remove("invalid");
      counter.textContent = `至少 5 个字 · ${reason.value.length} / 500`;
    });
    update();
    const presets = h(
      "div",
      { class: "presets" },
      [7, 30, 90, 180]
        .filter((d) => d <= maxDays)
        .map((d) => h("button", { type: "button", class: "chip", onclick: () => ((days.value = String(d)), update()) }, `${d} 天`)),
    );
    const [riskText, riskTone] = RISK[highest];

    openDrawer("g-title", (close) =>
      h(
        "form",
        {
          class: "form",
          novalidate: true,
          onsubmit: async (e) => {
            e.preventDefault();
            error.hidden = true;
            const d = Number.parseInt(days.value, 10);
            const problem = !(Number.isInteger(d) && d >= 1 && d <= maxDays) ? [days, `天数要在 1 到 ${maxDays} 之间。`] : reason.value.trim().length < 5 ? [reason, "请写一下申请理由，至少 5 个字。"] : null;
            if (problem) {
              problem[0].classList.add("invalid");
              problem[0].focus();
              error.textContent = problem[1];
              error.hidden = false;
              return;
            }
            submit.disabled = true;
            submit.textContent = "正在发起审批…";
            try {
              const res = await apiPost("/api/requests", {
                template_id: "policy",
                payload: { platform: acc.platform, account: acc.account, cloud_user: acc.cloud_user, days: d, policies: picked.map((p) => ({ type: p.type, name: p.name })) },
                reason: reason.value.trim(),
              });
              view.selected.clear();
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
          h("div", {}, h("div", { class: "eyebrow" }, "云账号权限"), h("h2", { id: "g-title" }, `申请 ${picked.length} 项权限`), h("div", { class: "drawer-tags" }, platformTag(acc.platform), h("span", { class: `pill ${riskTone}` }, highest === "low" ? "都是低风险" : `含${riskText}`))),
          h("button", { type: "button", class: "icon-btn", "aria-label": "关闭", onclick: close }, "✕"),
        ),
        h(
          "div",
          { class: "drawer-body" },
          h("div", { class: "drawer-meta" }, `${PLATFORM_NAME[acc.platform] || acc.platform} · ${acc.account_label || acc.account} · 子账号 ${acc.cloud_user}`),
          h(
            "ul",
            { class: "picked-list" },
            picked.map((p) => {
              const [t, tone] = RISK[p.risk] || RISK.medium;
              return h("li", {}, h("div", {}, h("div", { class: "perm-name" }, p.name, p.state === "owned" ? h("span", { class: "pill accent renew-tag" }, "续期") : null), p.description ? h("div", { class: "muted small" }, p.description) : null), h("span", { class: `pill ${tone}` }, t));
            }),
          ),
          highest === "high" ? h("div", { class: "banner warn" }, "包含高风险权限：审批人会重点核对，建议只申请真正需要的天数。") : null,
          h(
            "div",
            { class: "form-body" },
            h("div", { class: "field" }, h("label", { for: "g-days" }, "需要多少天"), h("div", { class: "inline" }, days, presets), h("div", { class: "hint" }, `最长 ${maxDays} 天（按所选权限里最严格的上限），到期自动收回。`)),
            h("div", { class: "field" }, h("label", { for: "g-reason" }, "申请理由"), reason, h("div", { class: "hint" }, counter)),
          ),
          h("div", { class: "preview" }, h("div", { class: "preview-label" }, "审批通过后"), preview),
        ),
        h("div", { class: "drawer-foot" }, error, h("div", { class: "drawer-actions" }, h("button", { type: "button", class: "btn ghost", onclick: close }, "取消"), submit), h("p", { class: "muted drawer-tip" }, `一次最多 ${max} 项。审批人在飞书里处理，结果会同步到「我的申请」。`)),
      ),
    );
    setTimeout(() => days.focus(), 0);
  }

  return { renderPermissions };
}
