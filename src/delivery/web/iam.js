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

import { ago, api, apiPost, fmtTime, h, mount } from "./core.js";

const API = "/api/admin/iam-attributes";
const FILE = API + "/file";

//: 对账的四类差异。**顺序就是严重程度** —— 最上面那两类是「现在就有人登错号 / 离职的人还有权限」，
//: 下面两类只是两边没同步。合成一类的话，最要紧的会被淹在一堆「少一条多一条」里
const DRIFT = [
  ["inactive", "已离职，云登录名还挂着", "crit", "点「确认离职」删掉登录名和云上的账号，数据不动"],
  ["different", "两边值不一样", "crit", "SSO 会用 IAM 那一边的值。确认哪个对，然后下发"],
  ["missing", "名册有，IAM 没有", "warn", "下发一次"],
  ["left", "IAM 有，名册没有", "", "先在名册这边确认这个人的情况"],
];

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

  if (data.offboard_error) nodes.push(h("div", { class: "banner warn" }, `离职记录读不了：${data.offboard_error}`));
  if ((data.offboard || []).length) nodes.push(offboardSection(data.offboard));
  nodes.push(reconcileSection(ui, data));
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
        h("li", {}, "IT 回复导入成功后，在「等 IT 导入」里点确认，那份全量快照成为新基线。"),
      ),
      h("p", { class: "hint" }, "确认必须等 IT 真的导入成功再点：确认即认定 IAM 侧已是这个状态，下一次只发和它的差异。没发出去的那一份用「作废」删掉，别留着。"),
    ),
  );
}

// ── 对账 ───────────────────────────────────────────────────────────────────
//
// 这是以前做不到的事：没有 IT 那个接口时，面板只能拿自己存的基线当真相，
// 而基线只记录「我们发过什么」，不记录「IT 那边最后变成了什么」。
// 中间任何一次人工导入出错，两边就永久漂移，而且没有任何地方看得出来。

function reconcileSection(ui, data) {
  const box = h("div", { class: "card card-pad" });
  // 上次对账的结果**页面一打开就显示**，不用点。按钮只是「重新对一次」
  const cached = ui.reconcile || (data && data.reconcile) || null;
  const btn = h("button", { type: "button", class: "btn small" },
    cached ? "重新对账" : "开始对账");
  const run = async () => {
    btn.disabled = true;
    btn.textContent = "正在读 IAM…";
    box.replaceChildren(h("p", { class: "hint" }, "在调 IT 的接口，几秒钟。"));
    try {
      const report = await apiPost(API, { op: "reconcile" });
      ui.reconcile = report;
      box.replaceChildren(...reconcileBody(report));
    } catch (e) {
      box.replaceChildren(h("div", { class: "banner crit" }, h("p", {}, e.message)));
    }
    btn.disabled = false;
    btn.textContent = "重新对账";
  };
  btn.addEventListener("click", run);
  if (cached) box.replaceChildren(...reconcileBody(cached));
  else box.replaceChildren(h("p", { class: "hint" }, "读 IAM 侧现在的 cloud_accounts，和名册比对。只读。"));
  return h(
    "section",
    { class: "group" },
    h("div", { class: "group-label" }, "和 IAM 对账", btn),
    box,
  );
}

// 离职的人的云账号：检测到离职已自动停用（关登录、禁 AK）的，和通讯录里找不到、
// 等人判断的。「确认删除」删云上的号，**不删任何数据**；「恢复」把停用时关掉的开回去，
// 之后不再自动停这个号。
const CLOUD = { aliyun: "阿里", volcano: "火山" };

function offboardSection(items) {
  const rows = items.map((r) => {
    const out = h("span", { class: "hint" });
    const del = h("button", { type: "button", class: "btn tiny", hidden: !!r.unverified }, "确认删除");
    const keep = h("button", { type: "button", class: "btn tiny ghost" },
      r.state === "disabled" ? "恢复" : "没离职");
    const act = async (op, ask) => {
      if (!window.confirm(ask)) return;
      del.disabled = true;
      keep.disabled = true;
      try {
        await apiPost(API, { op, key: `${r.platform}/${r.account}/${r.user}` });
        del.remove();
        keep.remove();
        out.replaceChildren(h("span", { class: "good-text" },
          op === "offboard_delete" ? "已删除云账号，数据没动" : "已恢复"));
      } catch (e) {
        del.disabled = false;
        keep.disabled = false;
        out.replaceChildren(h("span", { class: "recon-bad" }, e.message));
      }
    };
    del.addEventListener("click", () => act("offboard_delete",
      `删除 ${r.person} 的${CLOUD[r.platform] || r.platform}账号 ${r.user}？\n\n`
      + "会删掉这个云账号本身（先移出用户组、摘掉策略、删 AK）。他在桶里的文件、数据集、实例都不动。删了不能恢复。"));
    keep.addEventListener("click", () => act("offboard_restore",
      r.state === "disabled"
        ? `恢复 ${r.user}？会把停用时关掉的登录和 AK 开回去，之后不再自动停这个号。`
        : `${r.person} 没离职？这条会从待确认里拿掉。`));
    const state = r.state === "disabled" ? h("span", { class: "pill warn" }, "已停用") : h("span", { class: "pill" }, "未停用");
    return h("div", { class: "recon-row" },
      h("div", {}, h("b", {}, r.person || r.user), " ", state, " ",
        h("code", {}, `${CLOUD[r.platform] || r.platform} ${r.user}`)),
      h("div", { class: "hint" }, `${r.signal || ""} · ${fmtTime(r.at)}`),
      r.left ? h("div", { class: "recon-bad" }, `上次没删干净：${r.left.join("；")}`) : null,
      r.incomplete ? h("div", { class: "recon-bad" }, `停用没做完，下一轮会再试：${r.incomplete}`) : null,
      r.unverified ? h("div", { class: "recon-bad" }, "名册里这个号不归他，面板不删。核实后到云控制台处理。") : null,
      h("span", { class: "recon-act" }, del, keep, out));
  });
  return h("section", { class: "group" },
    h("div", { class: "group-label" }, `离职人员的云账号，待确认删除 ${items.length}`),
    h("p", { class: "hint pad" }, "检测到离职会自动停用（关登录、禁 AK）。通讯录里找不到的只提醒，不自动停。确认后删号，数据一律不动。"),
    ...rows);
}

// 「确认离职」= 管理员用自己这一下顶替「名册里也没有」那个信号。
// **服务端会重新读一次 IAM 核对**，所以这个按钮不能凭空删掉一个在职的人。
function reclaimButton(d) {
  const btn = h("button", { type: "button", class: "btn tiny" }, "确认离职，回收");
  const out = h("span", { class: "hint" });
  btn.addEventListener("click", async () => {
    if (!window.confirm(
      `确认 ${d.name || d.username} 已离职？\n\n`
      + `删掉他在 ${d.app} 的登录名 ${d.theirs}，并删除云上的账号。\n`
      + `他在桶里的文件、数据集、实例都不动。删了不能恢复。`
    )) return;
    btn.disabled = true;
    btn.textContent = "回收中…";
    try {
      const r = await apiPost(API, { op: "reclaim", union_id: d.union_id, app: d.app });
      btn.remove();
      out.replaceChildren(h("span", { class: "good-text" },
        `已删除登录名${r.previous ? ` ${r.previous}` : ""}。${r.cloud || ""}`));
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "确认离职，回收";
      out.replaceChildren(h("span", { class: "recon-bad" }, e.message));
    }
  });
  return h("span", { class: "recon-act" }, btn, out);
}

// 到点它会自己回到待办里，所以不需要「忽略」那个选项。
function snoozeButton(d) {
  const btn = h("button", { type: "button", class: "btn tiny ghost" }, "稍后处理");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await apiPost(API, { op: "snooze", union_id: d.union_id, app: d.app, hours: 3 });
      btn.replaceWith(h("span", { class: "hint" }, "3 小时后再提醒"));
    } catch (e) {
      btn.disabled = false;
      btn.after(h("span", { class: "recon-bad" }, e.message));
    }
  });
  return btn;
}

function reconcileBody(report) {
  const out = [];
  if (report.checked_at) {
    out.push(h("p", { class: "hint" },
      `上次对账 ${ago(report.checked_at)}`,
      report.snoozed ? h("span", {}, ` · ${report.snoozed} 条已稍后处理` ) : null));
  }
  for (const app of report.apps || []) {
    const head = h(
      "div",
      { class: "recon-head" },
      h("b", {}, app.app || app.scope),
      h("span", { class: "hint" }, `IAM ${app.theirs} 人 · 名册 ${app.compared} 人可比对`),
    );
    if (app.error) {
      out.push(head, h("div", { class: "banner crit" }, h("p", {}, app.error)));
      continue;
    }
    const drift = app.drift || [];
    const blind = app.blind || [];
    out.push(head);
    if (!drift.length && !blind.length) {
      out.push(h("p", { class: "recon-ok" }, "两边一致。"));
      continue;
    }
    for (const [kind, label, tone, what] of DRIFT) {
      const rows = drift.filter((d) => d.kind === kind);
      if (!rows.length) continue;
      out.push(
        h("div", { class: "recon-kind" },
          h("span", { class: `pill ${tone}` }, `${label} ${rows.length}`),
          h("span", { class: "hint" }, what)),
        h("ul", { class: "plist" }, rows.map((d) => h("li", { class: "recon-row" },
          h("b", {}, `${d.username || ""} ${d.name || ""}`.trim() || d.union_id),
          kind === "different"
            ? h("span", {}, ` IAM=${d.theirs}  名册=${d.ours}`)
            : h("span", {}, ` ${d.theirs || d.ours}`),
          // 只有「已离职」那一类能一键回收。另外三类要么该去发下发、要么该去改名册，
          // 给它们一个「确认」按钮只会让人点错
          kind === "inactive" ? reclaimButton(d) : null,
          kind === "inactive" ? snoozeButton(d) : null))),
      );
    }
    if (blind.length) {
      // **不能并进上面四类**：那些是两边都看得见但对不上，这些是我们这边根本没法比。
      // 混在一起的话，「对不上 0 条」会让人以为一致，而实际上有人压根没进过比对
      out.push(
        h("div", { class: "recon-kind" },
          h("span", { class: "pill warn" }, `没有 union_id ${blind.length}`),
          h("span", { class: "hint" }, "接口按 union_id 投递。没有它，这些人的登录名发不出去，也不进对账")),
        h("ul", { class: "plist" }, blind.map((r) => h("li", {},
          h("b", {}, r.name || r.email), h("span", {}, ` ${r.email} → ${r.value}`)))),
      );
    }
  }
  out.push(h("p", { class: "hint" }, `对账时间 ${fmtTime(report.checked_at)} · 共 ${report.total} 条对不上`));
  return out;
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
