// 管理后台「待办」页的渲染。
//
// 跑法：node --test tests/web/*.test.mjs
//
// 为什么这一层必须有测试：排序和分组算在服务端（delivery/todo.py），服务端用例钉得很死，
// 但**最后一米全在这里**，而这一米上的每一种错法都让页面「看起来完全正常」：
//   · 折叠写成截断 → 第 6 条以后的待办在页面上根本不存在，而首屏和正常时一模一样；
//   · 依据旧了的项不标灰 → 一份三天前的对账结果和今天刚采的长得一样确凿；
//   · errors 不渲染 → 「这一类没算出来」被读成「这一类没有问题」；
//   · 全空时不说话 → 空白页既像「今天没事」也像「接口挂了」。
// 服务端用例一条都抓不到这些：后端返回的 JSON 全程是对的。
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "#admin/todo", pathname: "/" };
globalThis.window.scrollTo = () => {};

const { renderTodo } = await import("../../src/delivery/web/todo.js");

function item(over = {}) {
  return {
    kind: "offboard_pending",
    group: "urgent",
    title: "李四：2 个云账号等着删",
    what: "面板还没停过这些号，他现在仍然能登。",
    action: "去处理",
    href: "#admin/iam",
    source: "离职记录",
    source_at: "",
    age_days: null,
    batch: true,
    weak: "",
    count: 2,
    ...over,
  };
}

function view(over = {}) {
  return {
    groups: [],
    counts: { urgent: 0, normal: 0, total: 0 },
    headline: "没有需要你处理的。",
    freshness: {},
    errors: [],
    fold_after: 5,
    ...over,
  };
}

function group(name, items, key = "urgent") {
  return { group: key, name, items };
}

/** 渲染一次，返回 #app 根节点。 */
function render(data) {
  renderTodo({ load: (_fetch, show) => show(data) });
  return globalThis.document.getElementById("app");
}

const all = (root) => [...root.walk()];
const byTag = (root, tag) => all(root).filter((n) => n.tagName === tag);
const hasClass = (n, c) => (n.className || "").split(/\s+/).includes(c);

test("每组一张卡，标题带组名和条数", () => {
  const root = render(
    view({
      headline: "今天有2 件要紧的。",
      groups: [group("要紧的", [item()]), group("顺手做的", [item({ kind: "keys" })], "chore")],
    }),
  );
  assert.match(root.textContent, /待办/);
  assert.match(root.textContent, /今天有2 件要紧的/);
  assert.match(root.textContent, /要紧的/);
  assert.match(root.textContent, /顺手做的/);
  assert.match(root.textContent, /1 项/);
});

test("一条待办要同时说清「发生了什么」和「不处理会怎样」", () => {
  // 只印标题的话，这一页就退化成一排状态词，而状态词不会让任何人动手
  const root = render(view({ groups: [group("要紧的", [item()])] }));
  assert.match(root.textContent, /李四：2 个云账号等着删/);
  assert.match(root.textContent, /他现在仍然能登/);
});

test("去处理按钮带上后端给的落点", () => {
  // href 由后端算（飞书卡片、命令行要跳到同一个地方）。前端自己拼的那天，
  // 三处会各跳各的，而错的那两处点开是一张「这页没有内容」
  const root = render(view({ groups: [group("要紧的", [item()])] }));
  const links = byTag(root, "A");
  assert.equal(links.length, 1);
  assert.equal(links[0].getAttribute("href"), "#admin/iam");
  assert.equal(links[0].textContent, "去处理");
});

test("没有落点的事项不渲染一个死链接", () => {
  const root = render(view({ groups: [group("要紧的", [item({ href: "", action: "" })])] }));
  assert.equal(byTag(root, "A").length, 0);
});

test("挂了多久要印出来，0 天也印", () => {
  // `age_days` 是数字，0 是合法值（今天刚出现）。用真值判断的话这一行会消失，
  // 而「没有这一行」和「这条是新的」在页面上分不开
  const root = render(view({ groups: [group("要紧的", [item({ age_days: 0 })])] }));
  assert.match(root.textContent, /挂了 0 天/);
  const older = render(view({ groups: [group("要紧的", [item({ age_days: 12 })])] }));
  assert.match(older.textContent, /挂了 12 天/);
});

// ── 折叠 ────────────────────────────────────────────────────────────────
// 「一屏看得完」是这一页存在的理由。但折叠**不能是截断**：藏起来的那几条
// 必须仍然在 DOM 里、点一下就出来，否则第 6 条以后的待办等于被删掉了。
function many(n) {
  return Array.from({ length: n }, (_, i) => item({ kind: `k${i}`, title: `第 ${i} 条` }));
}

test("超过 fold_after 的折起来，但一个按钮就能展开", () => {
  const root = render(view({ groups: [group("要紧的", many(8))], fold_after: 5 }));
  const more = byTag(root, "BUTTON").find((b) => /还有 3 项/.test(b.textContent));
  assert.ok(more, `没有展开按钮：${byTag(root, "BUTTON").map((b) => b.textContent)}`);
  // 折起来的那几条此刻不可见
  const box = all(root).find((n) => n.hidden === true);
  assert.ok(box, "被折叠的内容应当在一个 hidden 容器里");
  assert.match(box.textContent, /第 7 条/);
  // 但确实还在页面里，点一下就出来 —— 不是被截断
  more.dispatch("click");
  assert.equal(box.hidden, false);
});

test("正好等于 fold_after 时不折叠，也不出现空的展开按钮", () => {
  // 「还有 0 项」那种按钮是典型的差一错误，点了什么都不会发生
  const root = render(view({ groups: [group("要紧的", many(5))], fold_after: 5 }));
  assert.equal(byTag(root, "BUTTON").length, 0);
  assert.equal(all(root).filter((n) => n.hidden === true).length, 0);
});

test("后端没给 fold_after 时有个默认值，不会一条都不折", () => {
  const data = view({ groups: [group("要紧的", many(9))] });
  delete data.fold_after;
  const root = render(data);
  assert.ok(byTag(root, "BUTTON").some((b) => /还有 4 项/.test(b.textContent)));
});

// ── 依据的新鲜度 ────────────────────────────────────────────────────────
test("每份依据都列出来，旧的那份标黄", () => {
  // 「没发现问题」和「这次没查」在页面上必须分得开 —— 这一排小标签就是分界线
  const root = render(
    view({
      groups: [group("要紧的", [item()])],
      freshness: {
        "权限快照": { at: "2026-09-23T09:00:00+08:00", stale: false, note: "" },
        "IAM 对账": { at: "2026-08-01T09:00:00+08:00", stale: true, note: "" },
      },
    }),
  );
  const pills = all(root).filter((n) => hasClass(n, "pill"));
  assert.equal(pills.length, 2);
  const warn = pills.filter((p) => hasClass(p, "warn"));
  assert.equal(warn.length, 1, "只有旧的那份该标黄");
  assert.match(warn[0].textContent, /IAM 对账/);
  assert.match(root.textContent, /结论依据/);
});

test("这次没采到的源写「没采到」，不写一个假时间", () => {
  const root = render(
    view({ freshness: { "权限快照": { at: "", stale: true, note: "采集任务每 20 分钟跑一次" } } }),
  );
  assert.match(root.textContent, /权限快照 没采到/);
});

test("一份依据都没有时不渲染那张卡", () => {
  const root = render(view({ groups: [group("要紧的", [item()])] }));
  assert.equal(all(root).filter((n) => hasClass(n, "pill")).length, 0);
});

// ── 依据不足的事项 ──────────────────────────────────────────────────────
test("依据不足的事项标灰，并把理由写在那一条上", () => {
  // 标灰而不是删掉：提醒还要给，只是不该看起来和有凭有据的一样确凿
  const root = render(
    view({
      groups: [group("要紧的", [item({ weak: "对账数据旧了，先刷新一次再动手" })])],
    }),
  );
  const rows = all(root).filter((n) => hasClass(n, "todo-row"));
  assert.equal(rows.length, 1);
  assert.ok(hasClass(rows[0], "weak"), `这一条应当带 weak 类：${rows[0].className}`);
  assert.match(root.textContent, /先刷新一次再动手/);
});

test("依据充分的事项不带 weak 类", () => {
  // 反向锁：全都标灰等于没标
  const root = render(view({ groups: [group("要紧的", [item()])] }));
  const rows = all(root).filter((n) => hasClass(n, "todo-row"));
  assert.equal(rows.length, 1);
  assert.ok(!hasClass(rows[0], "weak"));
});

// ── 空和错 ──────────────────────────────────────────────────────────────
test("真的没事时明说，不是留一张白页", () => {
  const root = render(view());
  assert.match(root.textContent, /没有需要你处理的/);
});

test("有几类没算出来时挂横幅，而且绝不渲染成「今天没事」", () => {
  // 这是整页最危险的一种失败：全挂 = 一片干净
  const root = render(
    view({
      headline: "有几类没算出来，见下面的说明 —— 不代表没有要处理的事。",
      errors: ["离职待办没算出来：OffboardError", "密钥没算出来：DeliveryError"],
    }),
  );
  const banners = all(root).filter((n) => (n.className || "").includes("banner warn"));
  assert.equal(banners.length, 2);
  assert.match(root.textContent, /OffboardError/);
  assert.match(root.textContent, /不代表没有要处理的事/);
  assert.doesNotMatch(
    root.textContent,
    /没有需要你处理的/,
    "「没算出来」被渲染成了「没有需要你处理的」",
  );
});

test("既有事项又有错误时两样都出", () => {
  const root = render(
    view({ groups: [group("要紧的", [item()])], errors: ["密钥没算出来：DeliveryError"] }),
  );
  assert.match(root.textContent, /李四/);
  assert.match(root.textContent, /密钥没算出来/);
});

test("后端返回残缺（缺字段）时不炸页", () => {
  // 老前端 + 新后端、或者反过来。这一页是落地页，炸了等于面板打不开
  const root = render({});
  assert.ok(root.textContent.length >= 0);
  assert.match(root.textContent, /待办/);
});

test("分组里一条都没有时不渲染空卡", () => {
  const root = render(view({ groups: [group("要紧的", [])] }));
  assert.equal(all(root).filter((n) => hasClass(n, "todo-row")).length, 0);
});
