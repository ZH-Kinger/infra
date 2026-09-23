// 「我的账号」页上的「内部服务」一块（MLflow 这类自建服务）。
//
// 跑法：make test-web  （或 node --test tests/web/*.test.mjs）
//
// 为什么这一块只能在这一层锁：`personPage` 没有导出，服务端用例看不到它。
// 后端 `/api/me` 把 `detail.services` 返回了、前端不渲染（或渲染时炸了）的话，
// 服务端用例全绿，而页面上什么都没有 —— 「我有没有权限」这件事，人只会去页面上看。
//
// 下面几条里有三条曾经是红的（`fmtTime` 没 import、epoch 秒当毫秒、早返回把这一块跳过去），
// 都修好了 —— 留着是因为这三种坏法都**不报错、只是页面上少一块或整页「加载失败」**。
//
// 驱动方式：把 app.js 当页面 import 进来（它自己会 `boot()`），fetch 全换成假的。
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "#me", pathname: "/" };
globalThis.window.scrollTo = () => {};

//: 2030-01-01T00:00:00Z。`service_access.holdings` 给的是 **epoch 秒**（float）
const EXPIRES = 1893427200;
const ALI = { platform: "aliyun", account: "1234", user: "lisi", policies: [], resources: [] };
const FOREVER = [{ service: "mlflow", ticket: "REQ-20260101-DEADBEEF", expires_at_ts: 0 }];
const DATED = [{ service: "mlflow", ticket: "REQ-20260101-DEADBEEF", expires_at_ts: EXPIRES }];

//: 下一次 `/api/me` 返回什么
let ME = {};
//: app.js 注册的 hashchange 回调（换一份 `/api/me` 之后靠它重新渲染）
const hashListeners = [];
const realAdd = globalThis.window.addEventListener?.bind(globalThis.window);
globalThis.window.addEventListener = (name, fn) => {
  if (name === "hashchange") hashListeners.push(fn);
  if (realAdd) realAdd(name, fn);
};

globalThis.fetch = async (path) => ({
  ok: true,
  status: 200,
  json: async () => {
    if (path === "/api/session") {
      return { authenticated: true, user: { name: "王五" }, login_url: "/" };
    }
    if (path === "/api/me") return ME;
    if (path === "/api/requests") return { requests: [] };
    return {};
  },
});

function detail(extra) {
  return {
    person: { name: "王五", email: "wuji-wang@wuji-tech.com", union_id: "on_u1" },
    accounts: [],
    pending: [],
    summary: {},
    ...extra,
  };
}

function text() {
  const app = globalThis.document.getElementById("app");
  return [...app.walk()].map((n) => n._text || "").join("|");
}

/** 换一份 `/api/me` 再渲染一次。 */
async function render(me) {
  ME = me;
  for (const fn of hashListeners) fn();
  await new Promise((r) => setTimeout(r, 30));
  return text();
}

// app.js 在 import 时就 boot()
ME = detail({ accounts: [ALI], services: FOREVER });
await import("../../src/delivery/web/app.js");
await new Promise((r) => setTimeout(r, 40));

test("不限期的服务：和云账号列在一起，显示名不是那个短键", () => {
  const got = text();
  assert.ok(got.includes("内部服务"), got.slice(0, 400));
  // `mlflow` 这种短键是给网关比对用的，不是给人看的
  assert.ok(got.includes("MLflow 实验跟踪"), got.slice(0, 400));
  // 不限期要说清「离职时收回」，否则看起来像永久的
  assert.ok(got.includes("长期有效，离职时收回"), got.slice(0, 600));
});

test("没配显示名的服务原样显示，不能整行消失", async () => {
  const got = await render(
    detail({ accounts: [ALI], services: [{ service: "jupyter", ticket: "R", expires_at_ts: 0 }] }),
  );
  assert.ok(got.includes("jupyter"), got.slice(0, 400));
});

test("没有任何服务授权时不渲染这一块（不是渲染一个空壳）", async () => {
  const got = await render(detail({ accounts: [ALI], services: [] }));
  assert.ok(!got.includes("内部服务"), got.slice(0, 400));
  const missing = await render(detail({ accounts: [ALI] }));
  assert.ok(!missing.includes("内部服务"), "后端没返 services 这个键时也不能炸");
});

// **回归**：app.js 里调了 `fmtTime()` 却没 import 它（它在 core.js）—— 任何一张
// **带到期时间**的服务授权都会让「我的账号」整页变成「加载失败：ReferenceError」。
// 不限期那条不会踩到，所以人工点一次很容易漏过去。
test("有到期时间的服务不能把整页搞崩", async () => {
  const got = await render(detail({ accounts: [ALI], services: DATED }));
  assert.ok(!got.includes("加载失败"), got.slice(0, 200));
  assert.ok(got.includes("内部服务"), got.slice(0, 400));
});

// **回归**：`expires_at_ts` 是 epoch **秒**，而 `core.fmtTime(iso)` 是 `new Date(iso)` ——
// 直接把秒喂进去会被当成毫秒，2030 年的到期渲染成 1970-01-23，而页面不会报任何错。
// 字段名带 `_ts` 就是为了让这个单位在调用处看得见。
test("到期时间显示的是真的那个时间点（秒不是毫秒）", async () => {
  const got = await render(detail({ accounts: [ALI], services: DATED }));
  const want = new Date(EXPIRES * 1000).toLocaleString("zh-CN", {
    hour12: false,
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  assert.ok(got.includes(want), `期望出现 ${want}，实际：${got.slice(0, 400)}`);
  // 1970 那个坏法的签名：把秒当毫秒会落在 1970-01-23
  const wrong = new Date(EXPIRES).toLocaleString("zh-CN", { month: "numeric", day: "numeric" });
  assert.ok(!got.includes(`用到 ${wrong}`), got.slice(0, 400));
});

// **回归**：personPage 里有两个早返回（「还没有权限快照」「没有云账号」），内部服务
// 那一块原先排在它们后面 → 一个**没有云子账号**的人页面上什么都看不到，而这个功能的
// 立项理由恰恰是「最需要自建服务的人没有云子账号」。
test("没有云账号的人也要看得到 —— 而这批人正是自建服务的目标人群", async () => {
  const got = await render(detail({ accounts: [], services: FOREVER }));
  assert.ok(got.includes("内部服务"), got.slice(0, 400));
  assert.ok(got.includes("MLflow 实验跟踪"), got.slice(0, 400));
  assert.ok(!got.includes("加载失败"), got.slice(0, 200));
});

test("没有云账号、也没有服务授权时，还是那句「先申请开一个子账号」", async () => {
  const got = await render(detail({ accounts: [], services: [] }));
  assert.ok(got.includes("你目前没有阿里云或火山账号"), got.slice(0, 400));
  assert.ok(!got.includes("内部服务"), got.slice(0, 400));
});
