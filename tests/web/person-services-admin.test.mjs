// 同一页的**管理员视角**（`#person=<union_id>`）。本人视角在 person-services.test.mjs。
//
// 为什么要单独一个文件：`state.session` 在 boot 时取一次就定了，同一个进程里没法
// 从「本人」切到「管理员」—— 而 node --test 每个文件一个进程，所以换个文件最省事。
//
// 这一层锁两件事，都是「页面说了假话」而不是「页面报错」那一类：
//
//   · **那句离职警告贴在每张服务卡上**。它原先挂在分组顶部、写的是「下面的自建服务」，
//     而 `.accounts` 是 auto-fill 网格 —— 服务卡完全可能落在某张云账号卡**右边**，
//     那句话就指不到任何东西。它是「页面上列着、而『确认离职』不碰它」这个陷阱
//     在下一批修好之前唯一的防线，所以它的位置不能靠网格换行的运气。
//   · **组标题不能替管理员说「我的」**：管理员打开张三的页面时 H1 是「张三」，
//     底下一组标着「我的账号」就是在说别人的号是自己的。
//     这里锁的是「不出现『我的』」，不锁具体叫什么 —— 名字是文案，会变。
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "#person=on_u1", pathname: "/" };
globalThis.window.scrollTo = () => {};

const ALI = { platform: "aliyun", account: "1234", user: "lisi", policies: [], resources: [] };
const SERVICES = [
  { service: "mlflow", ticket: "REQ-1", expires_at_ts: 0 },
  { service: "jupyter", ticket: "REQ-2", expires_at_ts: 0 },
];

let DETAIL = {};
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
      // `role: "admin"` 才走得到 `#person=` 那条路由（否则被踢回本人视角）
      return { authenticated: true, role: "admin", user: { name: "管理员" }, login_url: "/" };
    }
    if (path.startsWith("/api/admin/people/")) return DETAIL;
    return {};
  },
});

function detail(extra) {
  return {
    person: { name: "张三", email: "zhangsan@wuji-tech.com", union_id: "on_u1" },
    accounts: [],
    pending: [],
    summary: {},
    binding: "union_id",
    ...extra,
  };
}

const all = (n) => [...n.walk()];
const hasClass = (n, c) => String(n.className || "").split(/\s+/).includes(c);
const root = () => globalThis.document.getElementById("app");
const pageText = () => root().textContent;

function serviceCards() {
  return all(root()).filter(
    (n) => hasClass(n, "card") && hasClass(n, "acct") && n.textContent.includes("自建服务"),
  );
}

function accountCards() {
  return all(root()).filter(
    (n) => hasClass(n, "card") && hasClass(n, "acct") && !n.textContent.includes("自建服务"),
  );
}

async function render(next) {
  DETAIL = next;
  for (const fn of hashListeners) fn();
  await new Promise((r) => setTimeout(r, 30));
  return pageText();
}

DETAIL = detail({ accounts: [ALI], services: SERVICES });
await import("../../src/delivery/web/app.js");
await new Promise((r) => setTimeout(r, 40));

test("前提：管理员视角确实渲染出了这个人的卡", () => {
  assert.equal(serviceCards().length, 2, pageText().slice(0, 400));
  assert.equal(accountCards().length, 1, pageText().slice(0, 400));
  assert.ok(!pageText().includes("加载失败"), pageText().slice(0, 200));
});

test("那句「确认离职不会自动收回」贴在**每一张**服务卡上", () => {
  const cards = serviceCards();
  for (const card of cards) {
    assert.ok(
      card.textContent.includes("确认离职"),
      `这张服务卡上没有那句警告：${card.textContent}`,
    );
  }
  // **不能只出现一次**：挂在分组顶部时它写的是「下面的自建服务」，而网格会换行 ——
  // 一旦服务卡落在云账号卡右边，那句话就指不到任何东西
  const hits = all(root()).filter((n) => (n._text || "").includes("确认离职"));
  assert.equal(hits.length, cards.length, "一张卡一条，不是整组一条");
});

test("警告只贴在服务卡上，不贴到云账号卡上", () => {
  // 云账号那条路「确认离职」是真的会收的 —— 在它上面说「不会自动收回」是反过来的假话
  for (const card of accountCards()) {
    assert.ok(!card.textContent.includes("确认离职"), card.textContent);
  }
});

test("组标题不替管理员说「我的」", () => {
  const labels = all(root())
    .filter((n) => hasClass(n, "group-label"))
    .map((n) => n.textContent);
  assert.ok(labels.length, "一个组都没有？");
  for (const label of labels) {
    assert.ok(!label.includes("我的"), `管理员视角的组标题在说别人的号是自己的：${label}`);
  }
});

test("管理员视角不给「申请更多权限」入口（那是本人才有的动作）", () => {
  const links = all(root()).filter((n) => n.getAttribute?.("href") === "#permissions");
  assert.equal(links.length, 0, pageText().slice(0, 400));
});

test("这个人只有服务、没有云账号时，警告照样一张卡一条", async () => {
  await render(detail({ accounts: [], services: SERVICES }));
  const cards = serviceCards();
  assert.equal(cards.length, 2, pageText().slice(0, 400));
  for (const card of cards) {
    assert.ok(card.textContent.includes("确认离职"), card.textContent);
  }
});

test("没有服务授权的人页面上不该出现那句警告（它说的是不存在的东西）", async () => {
  const got = await render(detail({ accounts: [ALI], services: [] }));
  assert.equal(serviceCards().length, 0, got.slice(0, 400));
  assert.ok(!got.includes("确认离职"), got.slice(0, 400));
});
