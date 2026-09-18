// 申请页切换分类时的渲染：一次点击只能引起**一次**布局变化。
//
// 背景：「找不到需要的权限？」那张提示卡只在「云账号权限」一栏出现。它的显示/隐藏原先挂在
// 分类按钮的 click 上、用 setTimeout 延后一个宏任务切，于是切换分类时页面会先按新列表重排
// 一次、下一帧再因为这张卡的出现/消失重排第二次 —— 用户看到的就是「整个界面闪一下」。
// 这类 bug 改回去不会有任何报错，只能靠断言「点完当场就是终态」锁住。
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "", pathname: "/" };

const { requestRoutes } = await import("../../src/delivery/web/requests.js");
const REPO = fileURLToPath(new URL("../..", import.meta.url));

/** 申请页收到的是 /api/requests/options 的形状：public() 再加上可申请状态。 */
function options() {
  const raw = JSON.parse(
    execFileSync(
      "python3",
      [
        "-c",
        `import json
from delivery import catalog
c = catalog.load("tests/web/fixture-templates.json")
print(json.dumps([t.public() for t in c.templates], ensure_ascii=False))`,
      ],
      { cwd: REPO, env: { ...process.env, PYTHONPATH: "src" }, encoding: "utf8" },
    ),
  );
  return raw.map((o) => ({ ...o, state: "available", state_note: "", available: true, request_id: "", account_label: "" }));
}

function page() {
  const { applyPage } = requestRoutes({ load: () => null, errorView: () => null });
  const nodes = applyPage(options(), []);
  const root = { children: nodes, walk: function* () { for (const c of this.children) { yield c; if (c.walk) yield* c.walk(); } } };
  const all = [...root.walk()];
  const hint = nodes.find((n) => n.className === "card hint-card");
  const tabs = all.filter((e) => e.tagName === "BUTTON" && e.attrs.role === "tab");
  return { nodes, all, hint, tabs };
}

function tabFor(tabs, title) {
  const hit = tabs.find((b) => b.textContent.includes(title));
  assert.ok(hit, `找不到「${title}」这一栏`);
  return hit;
}

test("三种申请类型都渲染出了分类按钮", () => {
  const { tabs } = page();
  // 按钮里套了标题和计数，textContent 会把它们连起来（真实 DOM 就是这样），
  // 所以按包含判，别指望某个节点的文本正好等于标题
  const texts = tabs.map((b) => b.textContent);
  for (const t of ["云账号权限", "访问凭证", "资源开通"]) {
    assert.ok(texts.some((x) => x.includes(t)), `少了「${t}」，实际有 ${JSON.stringify(texts)}`);
  }
});

test("从「云账号权限」切到「访问凭证」，提示卡当场就藏起来，不拖到下一帧", () => {
  const { hint, tabs } = page();
  assert.ok(hint, "提示卡没渲染出来");
  // 默认停在第一个有可申请项的分类，也就是「云账号权限」→ 卡片可见
  assert.equal(hint.hidden, false, "权限那一栏该显示提示卡");

  tabFor(tabs, "访问凭证").dispatch("click");
  // **同步**断言：不给 setTimeout / 微任务任何机会
  assert.equal(hint.hidden, true, "切到凭证后提示卡应当立刻隐藏（延后隐藏 = 页面闪一下）");

  tabFor(tabs, "资源开通").dispatch("click");
  assert.equal(hint.hidden, true);

  tabFor(tabs, "云账号权限").dispatch("click");
  assert.equal(hint.hidden, false, "切回权限后提示卡应当立刻出现");
});

test("重复点当前分类不做任何事", () => {
  const { hint, tabs } = page();
  tabFor(tabs, "访问凭证").dispatch("click");
  const before = hint.hidden;
  tabFor(tabs, "访问凭证").dispatch("click");
  assert.equal(hint.hidden, before);
});
