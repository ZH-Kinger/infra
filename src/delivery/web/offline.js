// 管理后台「人工登记」：没有采集接口的平台（九章），账号名单由管理员粘贴登记。
//
// 流程和人手工做的一样：去平台控制台把用户列表整页复制 → 粘进来 → **先看差异** → 再保存。
// 保存是**整体替换**：粘进来的就是完整名单，不在里面的就是没了。所以预览必须先于保存，
// 移除太多还要额外勾一下「确认移除」—— 粘漏半页的后果是半个名单从名册里消失。
//
// 手机号那一列服务端直接丢掉，不保存（名册按邮箱认人，用不上它）。
//
// 安全约定同全站：接口返回的字段只经 textContent / 属性赋值进入 DOM。

import { ApiError, api, apiPost, h, mount, platformTag } from "./core.js";

const API = "/api/admin/offline-accounts";
let pageLoad = null;

export function renderOffline({ load }) {
  pageLoad = load;
  return pageLoad(() => api(API), (data) => mount(page(data)));
}

function page(data) {
  const accounts = data.accounts || [];
  return h("div", {},
    h("header", { class: "masthead" },
      h("h1", {}, "人工登记"),
      h("p", { class: "lede" },
        "没有采集接口的平台（九章），账号名单靠这里登记。保存后下一轮刷新进名册、「我的账号」和离职检查。")),
    data.error ? h("div", { class: "banner warn" }, `登记表读不了：${data.error}`) : null,
    ...accounts.map((acc) => accountCard(acc, data.stale_days)),
    editor(accounts),
  );
}

function accountCard(acc, staleDays) {
  const head = h("div", { class: "asset-head" },
    h("div", { class: "chips" }, platformTag(acc.platform, acc.platform_name), h("b", {}, `主账号 ${acc.account}`)),
    h("span", { class: "asset-total" }, h("b", {}, String(acc.users.length)), " 人"));
  const age = acc.age_days == null ? "日期不明" : `${acc.age_days} 天前`;
  const note = h("p", { class: "pad muted" },
    `截至 ${acc.as_of}（${age}）· ${acc.source}`
    + (acc.login_prefix ? ` · 登录名以 ${acc.login_prefix} 开头` : ""));
  const stale = acc.stale
    ? h("div", { class: "banner warn pad-block" },
      `超过 ${staleDays} 天没更新。登记之后在平台上新开的号这里看不到，离职检查可能漏报 —— 重新复制一份名单保存。`)
    : null;
  const rows = acc.users.map((u) => h("tr", {},
    h("td", {}, h("code", {}, u.name)),
    h("td", {}, u.display_name || "—"),
    h("td", {}, u.email || "—"),
    h("td", {}, u.status || "—"),
    h("td", { class: "muted" }, u.created || "—")));
  const table = h("div", { class: "scroll" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["登录名", "姓名", "邮箱", "状态", "创建时间"].map((x) => h("th", {}, x)))),
    h("tbody", {}, ...rows)));
  const history = (acc.history || []).length
    ? h("p", { class: "pad muted" }, "最近修改：", ...acc.history.map((x, i) =>
      h("span", {}, `${i ? "；" : ""}${x.at} ${x.by}（+${x.added} −${x.removed}${x.changed ? ` ~${x.changed}` : ""}，共 ${x.total}）`)))
    : null;
  return h("section", { class: "card asset-card" }, head, note, stale, table, history);
}

function editor(accounts) {
  const first = accounts[0] || {};
  const platform = h("input", { id: "f-off-platform", class: "input narrow", value: first.platform || "jiuzhang", autocomplete: "off" });
  const account = h("input", { id: "f-off-account", class: "input narrow", value: first.account || "", autocomplete: "off", placeholder: "主账号名，如 wuji" });
  const prefix = h("input", { id: "f-off-prefix", class: "input narrow", value: first.login_prefix || "", autocomplete: "off", placeholder: "如 wuji-" });
  const text = h("textarea", { id: "f-off-text", class: "input", rows: "10", spellcheck: "false",
    placeholder: "在平台控制台的用户列表里全选复制，整页粘进来（表头、按钮文字不用删）" });
  const confirm = h("input", { type: "checkbox", class: "check", id: "f-off-confirm" });
  const confirmRow = h("label", { class: "check-row", for: "f-off-confirm", hidden: true }, confirm, "确认移除这些人（名单真的变了，不是粘漏了）");
  const out = h("div", {});
  const status = h("p", { class: "pad muted" });
  const saveBtn = h("button", { type: "button", class: "btn", disabled: true }, "保存");

  const body = () => ({
    platform: platform.value.trim(), account: account.value.trim(),
    login_prefix: prefix.value.trim(), text: text.value,
  });
  // 改了输入，之前的预览就作废了 —— 不然会保存一份和预览不一样的东西
  for (const el of [platform, account, prefix, text]) {
    el.addEventListener("input", () => { saveBtn.disabled = true; out.replaceChildren(); status.textContent = ""; });
  }

  async function preview() {
    status.textContent = "解析中…";
    try {
      const got = await apiPost(API, { ...body(), action: "preview" });
      out.replaceChildren(previewView(got));
      const many = got.diff.removed.length > Math.max(2, got.before * 0.3);
      confirmRow.hidden = !many;
      confirm.checked = false;
      saveBtn.disabled = !got.users.length;
      status.textContent = got.users.length ? "确认无误再保存。" : "一个人都没解析出来。";
    } catch (err) {
      status.textContent = err instanceof ApiError ? err.message : "预览失败";
    }
  }

  async function save() {
    saveBtn.disabled = true;
    status.textContent = "保存中…";
    try {
      const got = await apiPost(API, { ...body(), action: "save", allow_mass_remove: confirm.checked });
      status.textContent = `已保存：共 ${got.total} 人（+${got.diff.added.length} −${got.diff.removed.length}）。下一轮刷新进名册。`;
      text.value = "";
      out.replaceChildren();
      renderOffline({ load: pageLoad });
    } catch (err) {
      status.textContent = err instanceof ApiError ? err.message : "保存失败";
      saveBtn.disabled = false;
    }
  }
  saveBtn.addEventListener("click", save);

  return h("section", { class: "card" },
    h("div", { class: "group-label" }, "更新名单"),
    h("div", { class: "pad" },
      h("div", { class: "inline" },
        h("label", { for: "f-off-platform" }, "平台 "), platform,
        h("label", { for: "f-off-account" }, " 主账号 "), account,
        h("label", { for: "f-off-prefix" }, " 登录名前缀 "), prefix),
      text,
      h("p", { class: "muted" }, "保存是整体替换：粘进来的就是完整名单。手机号那一列不会保存。"),
      h("div", { class: "inline" },
        h("button", { type: "button", class: "btn ghost", onclick: preview }, "预览"), saveBtn),
      confirmRow),
    out, status);
}

function previewView(got) {
  const d = got.diff;
  const line = (label, names, tone) => (names.length
    ? h("p", { class: `pad ${tone || ""}` }, `${label} ${names.length}：${names.slice(0, 20).join("、")}${names.length > 20 ? " …" : ""}`)
    : null);
  return h("div", {},
    h("p", { class: "pad" }, `解析出 ${got.users.length} 人（原来 ${got.before} 人）。`),
    line("新增", d.added),
    line("移除", d.removed, "warn-text"),
    line("信息有变化", d.changed),
    ...(got.warnings || []).map((w) => h("p", { class: "pad muted" }, `⚠ ${w}`)));
}
