// 管理后台「IAM 属性表」：把发给 IT 的增量循环搬到浏览器里。
//
// 一轮是四步：预览这次要发什么 → 生成 → 下载增量发给 IT → IT 导入后点确认。
// 确认之后那份全量快照成为新基线，下一次只发变化的部分。
//
// 生成会写出**两份**文件，别发错：
//   增量（out_name）    含 remove 行，发给 IT 的就是它
//   全量快照（recorded）不含 remove（按设计，含 remove 的不能当基线），只用来当下一轮的比对基线
// 把全量快照发给 IT，等于漏掉所有删号 —— 离职的人属性会一直留在公司 IAM 里。
//
// 页面只读接口返回的字段，动作全部走 POST /api/admin/iam-attributes。

import { api, apiPost, fmtTime, h, mount } from "./core.js";

const API = "/api/admin/iam-attributes";
const FILE = API + "/file";

const ACTION = {
  set: ["写入", "accent"],
  remove: ["移除", "crit"],
  skip: ["跳过", ""],
};

export function renderIam({ load }) {
  return load(
    () => api(API),
    (data) => show(data, {}),
  );
}

function show(data, ui) {
  // 筛选按钮要在不重新请求的前提下重画，所以把这一份数据挂在 ui 上带着走
  const state = { ...(ui || {}), data };
  mount(page(data, state));
}

// 服务端把「一次删太多」拦下时，消息里带的是 CLI 的开关名；页面上换成勾选框的说法
function massRemoveBlocked(blocked) {
  return typeof blocked === "string" && blocked.includes("--allow-mass-remove");
}

// 这一轮的增量副本：和存档同名，存在存档旁边。out 是共享单文件、会被下次导出覆盖，
// 所以下载一律走这份副本，和「刚导出」那一瞬间的页面状态无关。
function incrementOf(data) {
  const row = (data.pending || []).find((p) => p.name === data.recorded);
  return (row && row.increment) || "";
}

function fileUrl(name) {
  return `${FILE}?name=${encodeURIComponent(name)}`;
}

function downloadLink(name, label) {
  return h("a", { class: "btn tiny ghost", href: fileUrl(name), download: name }, label || "下载 CSV");
}

function page(data, ui) {
  const rows = (data.increment && data.increment.rows) || [];
  const counts = (data.increment && data.increment.counts) || {};
  const pending = data.pending || [];
  const nodes = [
    h(
      "header",
      { class: "masthead" },
      h(
        "div",
        { class: "masthead-row" },
        h("h1", {}, "IAM 属性表"),
        h("button", { type: "button", class: "btn ghost small push", onclick: () => reload(ui) }, "重新计算"),
      ),
      h("p", { class: "lede" }, "把名册里的云账号写进公司 IAM 的 cloud_accounts 属性，SSO 才知道每个人该进哪个云上的号。这里生成要发给 IT 的增量文件，IT 导入后回来点确认。"),
    ),
  ];

  if (data.blocked && !massRemoveBlocked(data.blocked)) {
    nodes.push(h("div", { class: "banner crit" }, h("b", {}, "算不出这次的增量"), h("p", {}, data.blocked)));
  }

  nodes.push(baselineCard(data, pending));

  if (data.attributes_configured) {
    nodes.push(incrementSection(data, rows, counts, ui));
  }
  if (pending.length) {
    nodes.push(pendingSection(pending, ui));
  }
  nodes.push(howto());
  return nodes;
}

function baselineCard(data, pending) {
  const base = data.baseline;
  return h(
    "section",
    { class: "group" },
    h("div", { class: "group-label" }, "基线"),
    h(
      "div",
      { class: "card card-pad" },
      base
        ? h(
            "div",
            { class: "acct-head" },
            h(
              "div",
              { class: "acct-title" },
              h("span", { class: "acct-name mono" }, base.name),
              h("span", { class: "acct-sub" }, base.captured_at ? `确认于 ${fmtTime(base.captured_at)}` : "已确认"),
            ),
            h("span", { class: "push" }, downloadLink(base.name, "下载基线")),
          )
        : h(
            "div",
            {},
            h("b", {}, "还没有基线"),
            h("p", { class: "muted" }, "第一次会导出全量：IT 导入成功后回来点确认，它就成为基线，之后每次只发变化的部分。"),
          ),
      pending.length
        ? h("p", { class: "opt-note" }, `有 ${pending.length} 份存档还等着 IT 导入，确认之前算出的增量仍以上面这份基线为准。`)
        : null,
    ),
  );
}

function incrementSection(data, rows, counts, ui) {
  const blockedByMass = massRemoveBlocked(data.blocked);
  const nothing = data.can_export && !rows.length;
  const body = [
    h(
      "section",
      { class: "stats" },
      stat(counts.set || 0, "写入"),
      stat(counts.remove || 0, "移除", counts.remove ? "warn" : ""),
      stat(counts.skip || 0, "跳过"),
    ),
  ];

  for (const note of data.notes || []) {
    body.push(h("div", { class: "banner info" }, h("p", {}, note)));
  }

  if (blockedByMass) {
    body.push(massRemovePanel(data, ui));
  }

  if (nothing) {
    body.push(
      h(
        "div",
        { class: "card empty" },
        h("h2", {}, "没有要发的改动"),
        h("p", {}, "名册和基线一致，这次不用发给 IT。"),
      ),
    );
  } else if (rows.length) {
    body.push(rowsCard(rows, ui));
  }

  if (data.can_export) {
    body.push(exportBar(data, rows, ui));
  }
  if (data.recorded) {
    body.push(
      h(
        "div",
        { class: "banner good" },
        h("b", {}, "已生成"),
        h("p", {}, "下载下面这份增量发给 IT。导入成功后回到「等 IT 导入」里点确认，那一刻起它记录的状态成为新基线。"),
        h(
          "p",
          { class: "actions" },
          incrementOf(data) ? downloadLink(incrementOf(data), "下载增量（发给 IT）") : null,
        ),
        h("p", { class: "hint" }, `同时存了一份全量快照 ${data.recorded} 留作基线，不要把它发给 IT：全量快照不含删号行。刷新后仍可在下面「等 IT 导入」里重新下载。`),
      ),
    );
  }
  return h("section", { class: "group" }, h("div", { class: "group-label" }, "这次要发什么"), ...body);
}

function massRemovePanel(data, ui) {
  const shown = ui.allowMassRemove;
  return h(
    "div",
    { class: "banner warn" },
    h("b", {}, "这次有人要从公司 IAM 里移除，已先拦下"),
    h("p", {}, data.blocked),
    h(
      "p",
      { class: "actions" },
      shown
        ? h("span", { class: "muted" }, "下面已列出要移除的行，确认无误后勾选再生成。")
        : h(
            "button",
            {
              type: "button",
              class: "btn small",
              onclick: () => reload({ ...ui, allowMassRemove: true }),
            },
            "先看看要移除谁",
          ),
    ),
  );
}

function exportBar(data, rows, ui) {
  const blockedByMass = massRemoveBlocked(data.blocked);
  const needsOptIn = blockedByMass || (ui.allowMassRemove && rows.some((r) => r.action === "remove"));
  const check = h("input", { type: "checkbox", id: "iam-mass", checked: Boolean(ui.optedIn) });
  check.addEventListener("change", () => {
    ui.optedIn = check.checked;
    btn.disabled = needsOptIn && !ui.optedIn;
  });
  const btn = h(
    "button",
    {
      type: "button",
      class: "btn",
      disabled: (needsOptIn && !ui.optedIn) || (!rows.length && !blockedByMass),
    },
    "生成增量并存档",
  );
  const err = h("p", { class: "form-error" });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "生成中…";
    err.textContent = "";
    try {
      const next = await apiPost(API, { op: "export", allow_mass_remove: Boolean(ui.optedIn) });
      show(next, { allowMassRemove: ui.allowMassRemove });
    } catch (e) {
      err.textContent = e.message;
      btn.disabled = false;
      btn.textContent = "生成增量并存档";
    }
  });
  return h(
    "div",
    { class: "card card-pad" },
    needsOptIn
      ? h(
          "label",
          { class: "check", for: "iam-mass" },
          check,
          h("span", {}, "我已逐条核对过上面的移除行，确认这些人确实离职或销号"),
        )
      : null,
    h("div", { class: "actions" }, btn),
    h("p", { class: "hint" }, "生成后会写出 CSV 并存一份待确认存档；在 IT 确认导入之前，基线不变。"),
    err,
  );
}

const LIMIT = 200;

function rowsCard(rows, ui) {
  const filter = ui.filter || "all";
  const shown = filter === "all" ? rows : rows.filter((r) => r.action === filter);
  const chips = [
    ["all", "全部", rows.length],
    ["set", "写入", rows.filter((r) => r.action === "set").length],
    ["remove", "移除", rows.filter((r) => r.action === "remove").length],
    ["skip", "跳过", rows.filter((r) => r.action === "skip").length],
  ];
  const head = h(
    "div",
    { class: "chip-row" },
    chips.map(([key, label, n]) =>
      h(
        "button",
        {
          type: "button",
          class: `chip ${filter === key ? "active" : ""}`,
          disabled: !n && key !== "all",
          onclick: () => show(ui.data, { ...ui, filter: key }),
        },
        label,
        h("span", { class: "chip-n" }, String(n)),
      ),
    ),
  );
  const body = shown.slice(0, LIMIT);
  return h(
    "div",
    { class: "card" },
    h("div", { class: "toolbar pad" }, head),
    h(
      "div",
      { class: "scroll" },
      h(
        "table",
        { class: "people" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", {}, "动作"),
            h("th", {}, "姓名"),
            h("th", {}, "应用"),
            h("th", {}, "属性值"),
            h("th", {}, "说明"),
          ),
        ),
        h(
          "tbody",
          {},
          body.map((r) => {
            const [label, tone] = ACTION[r.action] || ["", ""];
            return h(
              "tr",
              {},
              h("td", { "data-label": "动作" }, h("span", { class: `pill ${tone}` }, label)),
              h("td", { "data-label": "姓名" }, h("span", { class: "pname" }, r.name || "—"), h("span", { class: "pmail" }, r.email || "")),
              h("td", { "data-label": "应用" }, r.app || "—"),
              h("td", { "data-label": "属性值" }, h("span", { class: "mono" }, r.value || "—")),
              h("td", { "data-label": "说明" }, r.problem || (r.match_by === "feishu_union_id" ? "" : r.match_by || "")),
            );
          }),
        ),
      ),
    ),
    shown.length > LIMIT
      ? h("p", { class: "opt-note" }, `只显示前 ${LIMIT} 行，共 ${shown.length} 行；完整内容请下载 CSV。`)
      : null,
  );
}

function pendingSection(pending, ui) {
  return h(
    "section",
    { class: "group" },
    h("div", { class: "group-label" }, "等 IT 导入"),
    h(
      "div",
      { class: "card list" },
      pending.map((p) => pendingRow(p, ui)),
    ),
  );
}

function pendingRow(p, ui) {
  const err = h("p", { class: "form-error" });
  const btn = h("button", { type: "button", class: "btn small" }, "IT 已导入，确认");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "确认中…";
    err.textContent = "";
    try {
      const next = await apiPost(API, { op: "confirm", name: p.name });
      show(next, {});
    } catch (e) {
      err.textContent = e.message;
      btn.disabled = false;
      btn.textContent = "IT 已导入，确认";
    }
  });
  const drop = h("button", { type: "button", class: "btn tiny ghost" }, "作废");
  drop.addEventListener("click", async () => {
    if (!window.confirm(`作废 ${p.name}？只有确认 IT 没有导入过这一份才能作废，否则基线会和公司 IAM 的实际状态对不上。`)) return;
    drop.disabled = true;
    err.textContent = "";
    try {
      const next = await apiPost(API, { op: "discard", name: p.name });
      show(next, {});
    } catch (e) {
      err.textContent = e.message;
      drop.disabled = false;
    }
  });
  const rows = p.rows >= 0 ? `${p.rows} 行` : "读不出行数";
  return h(
    "div",
    { class: "row" },
    h(
      "div",
      { class: "row-main" },
      h("div", { class: "row-title mono" }, p.name),
      h("div", { class: "row-sub" }, `全量快照 ${rows}　对着基线 ${p.baseline || "无（全量）"}`),
      p.increment ? null : h("p", { class: "opt-note" }, "这一份没有留存增量副本，请用「重新计算」重新生成后再发给 IT。"),
      err,
    ),
    h(
      "div",
      { class: "row-side" },
      p.increment ? downloadLink(p.increment, "下载增量（发给 IT）") : null,
      downloadLink(p.name, "下载快照"),
      drop,
      btn,
    ),
  );
}

function howto() {
  return h(
    "section",
    { class: "group" },
    h("div", { class: "group-label" }, "怎么走一轮"),
    h(
      "div",
      { class: "card card-pad" },
      h(
        "ol",
        { class: "plist" },
        h("li", {}, "看上面「这次要发什么」，确认写入和移除都对。"),
        h("li", {}, "点「生成增量并存档」，下载标着「发给 IT」的那份增量。"),
        h("li", {}, "把增量发给 IT，请他们导入公司 IAM。"),
        h("li", {}, "IT 回复导入成功后，在「等 IT 导入」里点确认——那份全量快照成为新基线。"),
      ),
      h("p", { class: "hint" }, "确认必须等 IT 真的导入成功再点：确认即认定 IAM 侧已是这个状态，下一次只发和它的差异。没发出去的那一份用「作废」删掉，别留着。"),
    ),
  );
}

function stat(n, label, tone) {
  return h("div", { class: `stat ${tone || ""}` }, h("span", { class: "n" }, String(n)), h("span", { class: "l" }, label));
}

function reload(ui) {
  const qs = ui.allowMassRemove ? "?allow_mass_remove=1" : "";
  api(API + qs).then(
    (data) => show(data, ui),
    (e) => {
      const app = document.getElementById("app");
      if (app) app.append(h("div", { class: "banner crit" }, h("p", {}, e.message)));
    },
  );
}
