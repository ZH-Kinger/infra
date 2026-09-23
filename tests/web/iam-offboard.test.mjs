// IAM 页「离职人员的云账号」一节：按钮发出去的 key 必须和后端 offboard.key_of 一致。
//
// 跑法：node --test tests/web/*.test.mjs
//
// 为什么要有这条：后端只认记录文件里的 key（`platform/account/user`），前端拼错一位
// （比如少了 account、或者把 person 当 user）的话，服务端一律 409「没有这条离职记录」——
// 页面上每一个「确认删除」都点不成，而服务端用例全绿。另外两件事也只能在这一层锁：
//   · 取消确认框 = 什么请求都不发（删号不可恢复）；
//   · 按钮文案随状态变：已停用的是「恢复」，没停用的嫌疑人是「没离职」。
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "", pathname: "/" };
globalThis.window.scrollTo = () => {};

const { renderIam } = await import("../../src/delivery/web/iam.js");

const DISABLED = {
  platform: "aliyun",
  account: "1000000000000001",
  user: "lisi",
  person: "李四",
  signal: "IT 的 IAM 标记离职",
  at: "2026-09-22T10:00:00+0800",
  state: "disabled",
  login: true,
  keys: ["LTAI1"],
};
const SUSPECT = {
  platform: "volcano",
  account: "2000000001",
  user: "wangwu",
  person: "王五",
  signal: "飞书通讯录里找不到（没有自动停用）",
  at: "2026-09-22T10:00:00+0800",
  state: "suspect",
  login: false,
  keys: [],
  left: ["摘策略 X：Throttling"],
};

function render(offboard, extra = {}) {
  renderIam({ load: (_fetch, show) => show({ pending: [], offboard, ...extra }) });
  return globalThis.document.getElementById("app");
}

function all(root) {
  return [...root.walk()];
}

function buttons(root, text) {
  return all(root).filter((n) => n.tagName === "BUTTON" && n.textContent === text);
}

function captureFetch(respond = () => ({ ok: true, status: 200, json: async () => ({}) })) {
  const sent = [];
  globalThis.fetch = async (path, init) => {
    sent.push({ path, method: init.method, body: init.body ? JSON.parse(init.body) : null });
    return respond();
  };
  return sent;
}

test("没有待办就不渲染这一节", () => {
  const root = render([]);
  assert.ok(!root.textContent.includes("离职人员的云账号"));
  assert.equal(buttons(root, "确认删除").length, 0);
});

test("每条记录一组按钮，文案随状态变", () => {
  const root = render([DISABLED, SUSPECT]);
  assert.ok(root.textContent.includes("离职人员的云账号，待确认删除 2"));
  assert.equal(buttons(root, "确认删除").length, 2);
  assert.equal(buttons(root, "恢复").length, 1);
  assert.equal(buttons(root, "没离职").length, 1);
  // 状态标签 = 事实 + 谁说的。面板并不知道云上此刻什么样，所以「没停过」只能说成
  // 面板没做过这个动作，不能写成「未停用」那种替云上下的断言 —— 那句话在
  // 「有人已经在控制台停了」的时候是错的，而错的方向恰好是让人以为还有敞口
  const pills = all(root).filter((n) => (n.className || "").includes("pill"));
  const labels = pills.map((p) => p.textContent);
  assert.ok(labels.some((t) => t.includes("已停用")), labels.join(" / "));
  assert.ok(
    labels.some((t) => t.includes("面板") && !t.includes("已停用")),
    `没停用的那条要说清是「面板没停过」：${labels.join(" / ")}`,
  );
  assert.ok(!labels.includes("未停用"), "「未停用」是在替云上做断言");
  assert.ok(root.textContent.includes("上次没删干净：摘策略 X：Throttling"));
});

test("确认删除发的 key 是 platform/account/user", async () => {
  const root = render([DISABLED]);
  const sent = captureFetch();
  globalThis.window.confirm = (msg) => {
    assert.ok(msg.includes("lisi") && msg.includes("不动"), msg);
    return true;
  };
  await buttons(root, "确认删除")[0].dispatch("click");
  assert.deepEqual(sent, [
    {
      path: "/api/admin/iam-attributes",
      method: "POST",
      body: { op: "offboard_delete", key: "aliyun/1000000000000001/lisi" },
    },
  ]);
  // 「号删了、东西没动」这两半都必须说出来：只说前半句，管理员会以为数据也一起没了，
  // 于是不敢点；只说后半句则看不出号到底删没删
  assert.ok(root.textContent.includes("已删除云账号"), root.textContent);
  assert.ok(root.textContent.includes("没动"), root.textContent);
});

test("恢复 / 没离职 发 offboard_restore", async () => {
  const root = render([DISABLED, SUSPECT]);
  const sent = captureFetch();
  globalThis.window.confirm = () => true;
  await buttons(root, "恢复")[0].dispatch("click");
  await buttons(root, "没离职")[0].dispatch("click");
  assert.deepEqual(
    sent.map((s) => s.body),
    [
      { op: "offboard_restore", key: "aliyun/1000000000000001/lisi" },
      { op: "offboard_restore", key: "volcano/2000000001/wangwu" },
    ],
  );
});

test("取消确认框就什么都不发", async () => {
  const root = render([DISABLED]);
  const sent = captureFetch();
  globalThis.window.confirm = () => false;
  await buttons(root, "确认删除")[0].dispatch("click");
  await buttons(root, "恢复")[0].dispatch("click");
  assert.deepEqual(sent, []);
});

test("服务端拒绝时显示原因、按钮可以再点", async () => {
  const root = render([DISABLED]);
  captureFetch(() => ({ ok: false, status: 409, json: async () => ({ error: "没删干净：摘策略 X" }) }));
  globalThis.window.confirm = () => true;
  const del = buttons(root, "确认删除")[0];
  await del.dispatch("click");
  assert.ok(root.textContent.includes("没删干净：摘策略 X"));
  assert.equal(del.disabled, false);
});

test("停用没做完的显示 incomplete", () => {
  const root = render([{ ...DISABLED, incomplete: "ProvisionError: 超时" }]);
  assert.ok(root.textContent.includes("停用没做完，下一轮会再试：ProvisionError: 超时"));
});

test("离职记录读不了：页面照常出，顶上一条警告", () => {
  const root = render([], { offboard_error: "离职记录读不了 /x/offboard.json" });
  const banners = all(root).filter((n) => (n.className || "").includes("banner warn"));
  assert.equal(banners.length, 1);
  assert.ok(banners[0].textContent.includes("离职记录读不了 /x/offboard.json"));
  // 其余部分照常渲染
  assert.ok(root.textContent.includes("IAM 属性表"));
});

test("没有 offboard_error 时没有这条警告", () => {
  const root = render([DISABLED]);
  assert.ok(!root.textContent.includes("离职记录读不了"));
});
