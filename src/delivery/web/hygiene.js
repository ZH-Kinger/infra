// 管理后台「体检」：人走了号还在、AK 该轮换、AK 没人用、认不出属主的号。
//
// 和命令行 `delivery hygiene` 是同一份逻辑（server 端调 hygiene.build），这里只负责显示。
// **只读**：这一页上没有任何按钮会停用、删除或修改云上的东西 —— 处置一律人工做。
// 自动做的话，第一次误伤就会让所有人学会忽略这类通知，那比不做还糟。
//
// 两条显示规矩，改的时候别绕过：
//   · `skipped` 必须显眼。少算了一整类却不说，一份残缺清单看起来和一份干净清单一模一样。
//   · 飞书在职状态是**点按钮才查**的。那是几十个串行请求，挂在页面加载上会让后台无故卡十几秒。

import { api, fmtTime, h, mount } from "./core.js";

// 排在前面的是「出了事找不到人 / 人已经不该有这个号」，比密钥太旧更急
const TONE = { left: "crit", unknown: "warn", orphan: "warn", abandoned: "crit", stray_bucket: "warn", stray_dir: "warn", rotate: "crit", unused: "warn" };

//: 页面里的按钮也要走 app.js 的 load()，不能自己 fetch 了直接 mount：
//: load() 管两件这一页特别需要的事 —— ① 请求失败时画错误页（`?status=1` 是几十个
//: 串行飞书请求，本来就是最容易超时的那个），② 换页作废（十几秒后回来时人可能已经
//: 切到别的 tab 了，直接 mount 会把体检内容画到那一页上，而地址栏还停在那边）。
let pageLoad = null;

export function renderHygiene({ load }) {
  pageLoad = load;
  return fetchInto(false);
}

/** 取一次并重画。`withStatus` 为真时才去查飞书在职状态（慢，要人显式点）。 */
function fetchInto(withStatus) {
  const url = `/api/admin/hygiene${withStatus ? "?status=1" : ""}`;
  return pageLoad(() => api(url), (data) => mount(page(data)));
}

function page(data) {
  const sections = data.sections || [];
  const skipped = data.skipped || [];
  const nodes = [
    h("header", { class: "masthead" },
      h("div", { class: "masthead-row" },
        h("h1", {}, "体检"),
        h("button", { type: "button", class: "btn ghost small push", onclick: () => fetchInto(false) }, "重新检查")),
      h("p", { class: "lede" },
        data.captured_at
          ? `依据 ${fmtTime(data.captured_at)} 的权限快照。只出清单，这一页不会停用或删除任何东西。`
          : "还没有权限快照。管理员运行 delivery inventory collect 后这里才有内容。")),
  ];

  nodes.push(
    data.total
      ? h("div", { class: "banner warn" }, h("b", {}, `${data.total} 项待人看一眼`), "，处置请到云控制台或用命令行，面板不代劳。")
      : h("div", { class: "banner good" }, h("b", {}, "没有发现需要处理的")),
  );

  // 少算了哪一类要摆在最前面：不说的话，残缺清单和干净清单长得一样
  for (const note of skipped) nodes.push(h("div", { class: "banner crit" }, h("b", {}, "⚠ "), note));

  nodes.push(statusBar(data));

  nodes.push(h("section", { class: "stats" }, ...sections.map((s) =>
    h("div", { class: `stat ${s.count ? TONE[s.kind] || "" : ""}` },
      h("span", { class: "n" }, String(s.count)),
      h("span", { class: "l" }, shortTitle(s))))));

  for (const s of sections) {
    if (!s.count) continue;
    nodes.push(
      h("section", { class: "group" },
        h("div", { class: "group-label" }, `${s.title}（${s.count}）`),
        h("div", { class: "card list" },
          h("p", { class: "pad muted" }, s.note),
          ...s.items.map((it) =>
            h("div", { class: "health-row" },
              h("span", { class: `pill ${TONE[s.kind] || ""}` }, shortTitle(s)),
              h("div", { class: "opt-main" },
                h("div", { class: "opt-name" }, it.scope, it.owner ? h("span", { class: "pmail" }, it.owner) : null),
                h("p", { class: "opt-desc" }, it.why, it.detail ? `（${it.detail}）` : "")))))),
    );
  }
  return nodes;
}

const SHORT = { left: "人已离职", unknown: "查不到人", orphan: "无主账号", abandoned: "号已删除", stray_bucket: "没登记的桶", stray_dir: "没登记的目录", rotate: "该换密钥", unused: "闲置密钥" };
function shortTitle(s) {
  return SHORT[s.kind] || s.title;
}

/** 飞书在职状态这一栏：默认没查，按钮点了才查。查不了就说清楚为什么。 */
function statusBar(data) {
  if (data.status_error) {
    // 查询故障 ≠ 这些人离职 —— 说清楚是哪一种，否则会有人照着「没发现离职」放心
    return h("div", { class: "banner crit" }, h("b", {}, "在职状态没查成："), data.status_error, " 这一类本次没有结论，不代表没人离职。");
  }
  if (data.status_checked) {
    // **必须把「查了几个」说出来**：只有绑过 union_id 的人查得到（没登录过面板的人
    // 名册里就没有），「查了 12 人没发现离职」和「查了 60 人没发现离职」是两回事。
    // 光说一句「已查过」，会拿它盖住「其实一多半人根本没查」
    const missing = data.status_missing_uid || 0;
    return h("p", { class: "pad muted" },
      `已查 ${data.status_asked || 0} 人的飞书在职状态（结果缓存 10 分钟）。`,
      missing
        ? h("span", { class: "warn-text" }, ` 另有 ${missing} 人名册里没有 union_id，本次查不了 —— 他们离没离职这一页给不出结论。`)
        : null);
  }
  if (!data.status_available) {
    return h("p", { class: "pad muted" }, "没配 DELIVERY_FEISHU_APP_ID / SECRET，查不了在职状态，因此不判断谁离职了。");
  }
  return h("div", { class: "pad" },
    h("button", {
      type: "button", class: "btn ghost small",
      onclick: (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "查询中…";
        // 失败时 load() 会把整页换成错误页，这个按钮届时已经不在 DOM 里了；
        // 但请求成功而结果里带 status_error 的那条路径会重画本页，按钮自然复位
        fetchInto(true);
      },
    }, "查飞书在职状态"),
    h("p", { class: "opt-desc" }, "要逐个查名册里每个人，几十个请求、大约十几秒。不查就不判断离职 —— 拿不到状态却照算，等于把全公司报成离职。"));
}
