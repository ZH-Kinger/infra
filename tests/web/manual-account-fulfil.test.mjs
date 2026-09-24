// 九章 / TurboAI（曦望）开号单的「回填」抽屉。
//
// 跑法：node --test tests/web/*.test.mjs
//
// **为什么这一米必须有测试**：整条链上只有一个人类动作 —— 管理员在那个平台的控制台
// 把号建出来，回面板填登录名。服务端把 `fulfil` 这个动作对开账号单放开了，服务端用例
// 也钉得很死；但管理员看到的抽屉如果还是资源开通那一版（「实例 ID：i-bp1xxx」
// 「开通了什么：例：ECS 通用 2 核 8G」），他照着填的东西必然被登录名校验拒成 400，
// 而错误提示说的是「登录名不合规矩」—— 和他刚填的两栏都对不上号。
// 服务端一条用例都抓不到这个：后端返回的 JSON 全程是对的。
import assert from "node:assert/strict";
import test from "node:test";
import { Node } from "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "#admin/requests/REQ-1", pathname: "/" };
globalThis.window.scrollTo = () => {};

// `openDrawer` 会 `dialog.showModal()` / `dialog.close()`，最小 DOM 里没有这两样。
// 补在这里而不是改 dom.mjs：别的用例都不开抽屉，给共用桩加行为容易顺手改坏它们
Node.prototype.showModal = function showModal() {
  this.attrs.open = "";
};
Node.prototype.close = function close() {
  delete this.attrs.open;
  this.dispatch("close");
};

const { requestRoutes } = await import("../../src/delivery/web/requests.js");

/** 一张「待开通」的开号单，形状照 `requests_api.ticket_view`。 */
function ticket(over = {}) {
  const template = {
    id: "jiuzhang-new-user",
    kind: "account",
    platform: "jiuzhang",
    account: "wuji",
    title: "九章子账号",
    ...(over.template || {}),
  };
  return {
    id: "REQ-1",
    kind: "account",
    kind_label: "开账号",
    status: "fulfilling",
    result: "",
    summary: "开账号「九章子账号」",
    reason: "要在九章上跑训练",
    applicant: { name: "李四", email: "li.si@wuji.tech" },
    created_at: "2026-09-24T10:00:00+08:00",
    events: [{ event: "await_fulfil", label: "等待管理员开通", actor: "admin", at: "", note: "" }],
    approval: {},
    payload: {},
    template,
    platform: template.platform,
    ...over,
    actions: { fulfil: true, ...(over.actions || {}) },
  };
}

/** 渲染管理员视角的详情页，返回 #app。 */
function detail(data) {
  const { renderDetail } = requestRoutes({ load: (_fetch, show) => show({ request: data }), errorView: () => null });
  globalThis.__els.app = new Node("main");
  renderDetail(data.id, { admin: true, fresh: false });
  return globalThis.__els.app;
}

const all = (root) => [...root.walk()];

/** 点开回填按钮，返回抽屉里那张卡。 */
function drawer(data) {
  const root = detail(data);
  const btn = all(root).find((n) => n.tagName === "BUTTON" && /回填|登记开通结果/.test(n.textContent));
  assert.ok(btn, "详情页上没有回填按钮");
  globalThis.document.body.children = [];
  btn.dispatch("click");
  const dialog = globalThis.document.body.children.at(-1);
  assert.ok(dialog, "点了按钮没开出抽屉");
  return { btn, dialog, inputs: all(dialog).filter((n) => ["INPUT", "TEXTAREA"].includes(n.tagName)) };
}

test("九章开号单：按钮和抽屉说的都是「登录名」，不是「实例 ID」", () => {
  const { btn, dialog, inputs } = drawer(ticket());
  assert.match(btn.textContent, /回填登录名/);
  assert.match(dialog.textContent, /九章/);
  assert.match(dialog.textContent, /登录名/);
  // 这两栏是资源开通那一版的。留着的话管理员会照着填，然后被 400 拒掉
  assert.doesNotMatch(dialog.textContent, /实例 ID/);
  assert.doesNotMatch(dialog.textContent, /开通了什么/);
  assert.equal(inputs.length, 1, `回填只有一栏（登录名），实际 ${inputs.length} 栏`);
  assert.equal(inputs[0].attrs.placeholder, "wuji-zhangsan", "占位符要给一个真的能用的样子");
});

test("曦望开号单也走同一张抽屉，平台名跟着换", () => {
  const { btn, dialog } = drawer(
    ticket({ template: { id: "turboai-new-user", platform: "turboai", account: "Wuji-Algorithm@wuji.tech", title: "TurboAI 子账号" } }),
  );
  assert.match(btn.textContent, /回填登录名/);
  assert.match(dialog.textContent, /TurboAI/);
  assert.doesNotMatch(dialog.textContent, /九章/);
});

test("真云的资源开通单一个字没变", () => {
  // 对照组。少了它，「把所有 fulfil 都改成登录名版」也能让上面两条全绿 ——
  // 而那样实例 ID 就再也填不进去，资产页的归属永远指不到人
  const { btn, dialog, inputs } = drawer(
    ticket({ kind: "resource", kind_label: "资源开通", template: { id: "rds-free", kind: "resource", platform: "aliyun", account: "1000000000000001", title: "RDS 实例" } }),
  );
  assert.match(btn.textContent, /登记开通结果/);
  assert.match(dialog.textContent, /实例 ID/);
  assert.match(dialog.textContent, /开通了什么/);
  assert.equal(inputs.length, 2);
});

test("待开通那条横幅不对九章说「按 IaC 流程创建」", () => {
  // 九章的号不是 IaC 能开的东西。这么写的话，管理员会去翻一份根本不存在的流水线
  const root = detail(ticket());
  const banner = all(root).find((n) => /审批已通过，等待开通/.test(n.textContent) && (n.className || "").includes("banner"));
  assert.ok(banner, "没渲染出「等待开通」横幅");
  assert.match(banner.textContent, /九章/);
  assert.doesNotMatch(banner.textContent, /IaC/);
});

test("资源单的横幅照旧说 IaC", () => {
  const root = detail(ticket({ kind: "resource", template: { id: "rds-free", kind: "resource", platform: "aliyun", account: "1000000000000001", title: "RDS 实例" } }));
  const banner = all(root).find((n) => /审批已通过，等待开通/.test(n.textContent) && (n.className || "").includes("banner"));
  assert.match(banner.textContent, /IaC/);
});

test("状态卡上那句「接下来干什么」也分开说", () => {
  const manual = detail(ticket());
  const hint = all(manual).find((n) => (n.className || "") === "status-hint");
  assert.ok(hint, "没渲染出下一步提示");
  assert.match(hint.textContent, /回填登录名/);
  assert.doesNotMatch(hint.textContent, /实例信息/);

  const res = detail(ticket({ kind: "resource", template: { id: "rds-free", kind: "resource", platform: "aliyun", account: "1000000000000001", title: "RDS 实例" } }));
  const resHint = all(res).find((n) => (n.className || "") === "status-hint");
  assert.match(resHint.textContent, /实例信息/);
});

// ── 申请侧：人工平台的表单还在要一个会被丢掉的用户名 ───────────────────

/** `tpl.public()` 的形状 + 申请页补的那几个状态字段。 */
function option(over = {}) {
  return {
    id: "jiuzhang-new-user", kind: "account", kind_label: "开账号",
    platform: "jiuzhang", account: "wuji", title: "九章子账号", description: "",
    risk: "low", category: "", groups: [], max_hours: 0, sts_max_hours: 0,
    sts_available: false, caps: [], cap_labels: [], buckets: [], types: {},
    allow_prefix: false, whole_bucket: false, max_days: 0, service: "",
    options: [], cost_centers: [], cost_center_other: false, spec_hint: "",
    resource_type: "", region: "", automatic: false,
    username_pattern: "^[a-z][a-z0-9.-]{1,31}$", console_login: false,
    state: "available", state_note: "", available: true, request_id: "", account_label: "",
    ...over,
  };
}

function applyForm(o) {
  const { applyForm: build } = requestRoutes({ load: () => null, errorView: () => null });
  const form = build(o, [], () => {});
  const byId = new Map();
  for (const el of form.walk()) if (el.attrs && el.attrs.id) byId.set(el.attrs.id, el);
  const preview = [...form.walk()].find((e) => e.className === "preview-text");
  return { form, byId, preview };
}

test("对照：真云的开账号单照旧要用户名，预览照旧说「新建子账号」", () => {
  const { byId, preview } = applyForm(option({ id: "aliyun-new-user", platform: "aliyun", account: "1000000000000001", groups: ["grp-default"], console_login: true }));
  assert.ok(byId.get("f-username"), "真云那条必须还有用户名栏 —— 那个名字真的会拿去云上建号");
  byId.get("f-username").value = "zhang.san";
  byId.get("f-username").dispatch("input");
  assert.match(preview.textContent, /新建子账号 zhang\.san/);
});

// 服务端不收人工平台的 username（`flows._validate` 的人工分支返回 `{}`，审批摘要
// 也说「面板开不了这个平台的号」）。**申请表单必须同口径**：
//   · 留着那个必填的「子账号用户名」栏，申请人会被逼着编一个名字，而它会被直接丢掉；
//   · 预览那行是**申请人点提交前最后读到的一句**，读的人比审批摘要还多 ——
//     说「新建子账号 lisi」就是许了一个不会存在的名字（真实登录名是 `wuji-lisi`，
//     或者曦望那边管理员随手起的）。他拿 lisi 登不进去，还会以为是号没建好。
test("人工平台的申请表单不要用户名，预览也不许一个不会存在的名字", () => {
  const { byId, preview } = applyForm(option());
  assert.equal(byId.get("f-username"), undefined, "这个名字服务端不收，别逼申请人编一个");
  assert.doesNotMatch(preview.textContent, /新建子账号/);
  assert.match(preview.textContent, /九章/);
  // 光是不说谎还不够：得告诉申请人接下来会发生什么，否则他提交完不知道在等谁
  assert.match(preview.textContent, /面板开不了/);
  assert.match(preview.textContent, /回填/);
});

test("曦望的申请表单同样不要用户名", () => {
  // 对照：只认 jiuzhang 的话这条会红 —— 前端那张名单漏一个平台的典型漏法
  const { byId, preview } = applyForm(
    option({ id: "turboai-new-user", platform: "turboai", account: "Wuji-Algorithm@wuji.tech", title: "TurboAI 子账号" }),
  );
  assert.equal(byId.get("f-username"), undefined);
  assert.doesNotMatch(preview.textContent, /新建子账号/);
  assert.match(preview.textContent, /TurboAI/);
});

test("人工平台的表单没了用户名栏，照样提交得出去，body 里也没有 username", async () => {
  // 必填校验是跟着那一栏一起加的。**只删栏、不删校验**的话，表单会卡在一句
  // 「请填写子账号用户名。」上 —— 而页面上根本没有那一栏可填，申请人无路可走。
  //
  // 判据是「请求真的发出去了」，不是「没有 invalid 类」：校验失败时也可能只改文案
  // 不加类，那种写法下按类断言会永远绿
  const sent = [];
  const saved = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    sent.push({ path, body: JSON.parse(init.body) });
    return { ok: true, json: async () => ({ request: { id: "REQ-9" } }) };
  };
  try {
    const { form, byId } = applyForm(option());
    byId.get("f-reason").value = "要在九章上跑训练任务";
    byId.get("f-reason").dispatch("input");
    await form.dispatch("submit");
  } finally {
    globalThis.fetch = saved;
  }
  assert.equal(sent.length, 1, "校验没放行，压根没走到发请求那一步");
  assert.equal(sent[0].path, "/api/requests");
  assert.deepEqual(sent[0].body.payload, {}, "人工平台送空 payload —— 别凭空塞一个 username");
});

test("对照：真云的开账号单仍然把用户名送上去", () => {
  // 少了这条，「把 payload 一律清空」也能让上面那条绿 —— 而那样阿里那条路
  // 会拿着一个空用户名去云上建号
  const { form, byId } = applyForm(option({ id: "aliyun-new-user", platform: "aliyun", account: "1000000000000001" }));
  byId.get("f-username").value = "zhang.san";
  byId.get("f-username").dispatch("input");
  assert.deepEqual(form.readPayload(), { username: "zhang.san" });
});
