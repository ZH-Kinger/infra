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

// ── 结构断言的小工具 ─────────────────────────────────────────────────────
//
// 这一块的断言**尽量锁结构 + 关键信息，不锁整句文案**：文案会被反复微调
// （「长期有效，离职时收回」→「长期有效。离职时随账号一起收回，不会自己过期。」
// 就是一次），每次微调都红的用例，红的原因和它要保护的性质无关，久了就会被人
// 随手改绿。真正要保住的是：这块渲染出来了、是和云账号同一种卡、显示名是给人看的
// 那个、「有效期」那一栏说清了它不会自己过期。
const all = (n) => [...n.walk()];
const hasClass = (n, c) => String(n.className || "").split(/\s+/).includes(c);
const root = () => globalThis.document.getElementById("app");

/** 页面上的服务卡（按卡里那个「自建服务」标签认，云账号卡外壳和它一样）。 */
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

/** 卡里 `.acct-name` 的文本（= 给人看的那个名字）。 */
function nameOf(card) {
  const el = all(card).find((n) => hasClass(n, "acct-name"));
  return el ? el.textContent : "";
}

/** 服务卡所在的那一组（`section.group`）—— 用来断言它和云账号列在一起。 */
function groupOf(card) {
  let n = card;
  while (n && !(n.tagName === "SECTION" && hasClass(n, "group"))) n = n.parent;
  return n;
}

/** 一组里按出现顺序排下来的卡（云账号在前、自建服务在后）。 */
function cardsIn(group) {
  return all(group).filter((n) => hasClass(n, "card") && hasClass(n, "acct"));
}

/** 卡里标着 `label` 的那一节的正文（不含标题）。找不到返回 null。 */
function sectionOf(card, label) {
  for (const sec of all(card).filter((n) => hasClass(n, "section"))) {
    const head = sec.children.find((c) => hasClass(c, "section-label"));
    if (!head || head.textContent !== label) continue;
    return sec.children
      .filter((c) => c !== head)
      .map((c) => c.textContent)
      .join("");
  }
  return null;
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

test("不限期的服务：一张卡，显示名不是那个短键，有效期那栏说清它不会自己过期", () => {
  const got = text();
  const [card, ...rest] = serviceCards();
  assert.ok(card, `没渲染出服务卡：${got.slice(0, 400)}`);
  assert.equal(rest.length, 0, "一条授权一张卡");

  // `mlflow` 这种短键是给网关比对用的，不是给人看的
  assert.equal(nameOf(card), "MLflow 实验跟踪");
  assert.ok(!card.textContent.includes("mlflow"), card.textContent);

  // **不限期那句必须提到「离职」**：这是唯一要保住的信息 —— 不说的话人会以为
  // 它自己会过期（纳管进来的那批尤其），而实际上不会，只有离职才收。
  // 这里不锁整句，句子怎么写是文案的事
  const period = sectionOf(card, "有效期");
  assert.ok(period !== null, `没有「有效期」这一节：${card.textContent}`);
  assert.ok(period.includes("离职"), period);
  assert.ok(!period.includes("用到"), `不限期不该出现到期时间：${period}`);
});

test("服务卡和云账号卡是同一种卡（并排时看起来是同一类东西）", () => {
  const [svc] = serviceCards();
  const [acct] = accountCards();
  assert.ok(svc && acct, "这一条要两种卡都在才有意义");
  // 外壳、头部、名字都走同一套类名：换了其中一边，两块在页面上会开始长得不一样，
  // 而它们在流程上本来是同一类东西（同一套申请、同一套离职回收）
  assert.equal(svc.tagName, acct.tagName);
  assert.equal(svc.className, acct.className);
  for (const cls of ["acct-head", "acct-title", "chips", "acct-name", "section", "section-label"]) {
    assert.ok(
      all(svc).some((n) => hasClass(n, cls)),
      `服务卡缺 ${cls}（云账号卡有）`,
    );
    assert.ok(all(acct).some((n) => hasClass(n, cls)), `对照组自己就没有 ${cls}？`);
  }
});

// 组名从「内部服务」变成了「我的账号」，服务并进了云账号那一组。**这里不锁组名** ——
// 名字是文案，改一次红一次；要保住的性质是「这两种东西列在同一组里」：
// 对着这一页做判断的人（尤其管理员点「确认离职」）得一眼看全这个人手上有什么。
test("服务卡和云账号卡在同一组里，云账号在前、服务在后", async () => {
  await render(detail({ accounts: [ALI], services: FOREVER }));
  const [svc] = serviceCards();
  const [acct] = accountCards();
  const group = groupOf(svc);
  assert.ok(group, "服务卡应当在某个 section.group 里");
  assert.equal(group, groupOf(acct), "两种卡不在同一组里了");
  assert.deepEqual(cardsIn(group), [acct, svc], "云账号排前面，服务跟在后面");
});

test("卡上给得出「这张授权是哪来的」——点得进那张申请单", () => {
  const [card] = serviceCards();
  const origin = sectionOf(card, "来自哪张申请");
  assert.ok(origin !== null, card.textContent);
  assert.ok(origin.includes("REQ-20260101-DEADBEEF"), origin);
  const link = all(card).find((n) => n.tagName === "A" && n.textContent.includes("REQ-"));
  assert.equal(link.getAttribute("href"), "#request=REQ-20260101-DEADBEEF");
});

test("没有单号的授权不渲染「来自哪张申请」，也不留一个指向空处的链接", async () => {
  await render(
    detail({ accounts: [ALI], services: [{ service: "mlflow", ticket: "", expires_at_ts: 0 }] }),
  );
  const [card] = serviceCards();
  assert.ok(card, "没单号也要有卡");
  assert.equal(sectionOf(card, "来自哪张申请"), null);
  assert.equal(
    all(card).filter((n) => n.tagName === "A").length,
    0,
  );
});

// 后端今天**不返回** `url`，所以那个「进入 →」按钮恒不显示。锁住它是因为反过来更糟：
// 渲染出一个 href 为空的按钮，点了什么都不发生 —— 页面上有个死按钮比没有按钮更难查。
test("没有服务地址就不渲染「进入」按钮", async () => {
  await render(detail({ accounts: [ALI], services: FOREVER }));
  const [card] = serviceCards();
  assert.ok(!card.textContent.includes("进入"), card.textContent);
  assert.equal(all(card).filter((n) => hasClass(n, "linkbtn") && hasClass(n, "push")).length, 0);
});

test("以后带上服务地址时：https 才渲染，别的协议一律不给链接", async () => {
  await render(detail({ accounts: [ALI], services: [{ ...FOREVER[0], url: "https://mlflow.x/" }] }));
  const [ok] = serviceCards();
  const btn = all(ok).find((n) => hasClass(n, "linkbtn") && hasClass(n, "push"));
  assert.ok(btn, `带 url 时应当有「进入」按钮：${ok.textContent}`);
  assert.equal(btn.getAttribute("href"), "https://mlflow.x/");
  assert.ok(
    String(btn.getAttribute("rel") || "").includes("noopener"),
    `外链要带 noopener：${btn.getAttribute("rel")}`,
  );
  assert.equal(btn.getAttribute("target"), "_blank");

  // `javascript:` / `http:` / 乱写：`safeHttps` 压成空串，**整个按钮都不该出现**。
  // 判据不能是「href 是空串」—— 空 href 的 `<a>` 照样可点，点下去跳回当前页并把
  // fragment 抹掉（人被弹回首页），那和「没有按钮」是两回事
  for (const bad of ["javascript:alert(1)", "http://mlflow.x/", "不是网址", "//evil.example"]) {
    await render(detail({ accounts: [ALI], services: [{ ...FOREVER[0], url: bad }] }));
    const [card] = serviceCards();
    assert.ok(card, `${bad}：卡本身还得在`);
    const links = all(card).filter((n) => hasClass(n, "linkbtn") && hasClass(n, "push"));
    assert.equal(links.length, 0, `${bad} 不该渲染出「进入」按钮`);
    assert.ok(!card.textContent.includes("进入"), card.textContent);
  }
  await render(detail({ accounts: [ALI], services: FOREVER }));
});

test("没配显示名的服务原样显示，不能整行消失", async () => {
  const got = await render(
    detail({ accounts: [ALI], services: [{ service: "jupyter", ticket: "R", expires_at_ts: 0 }] }),
  );
  const [card] = serviceCards();
  assert.ok(card, `没渲染出服务卡：${got.slice(0, 400)}`);
  assert.equal(nameOf(card), "jupyter");
});

test("没有任何服务授权时一张服务卡都不渲染（不是渲染一个空壳）", async () => {
  // **按「有没有服务卡」判，不按组名判**：服务并进云账号那一组之后，
  // 「页面上没有『内部服务』四个字」恒为真 —— 那种断言再也红不了
  const got = await render(detail({ accounts: [ALI], services: [] }));
  assert.equal(serviceCards().length, 0, got.slice(0, 400));
  assert.equal(accountCards().length, 1, "云账号那张还得在，否则这条可能只是整页没渲染");

  const missing = await render(detail({ accounts: [ALI] }));
  assert.equal(serviceCards().length, 0, "后端没返 services 这个键时也不能炸");
  assert.ok(!missing.includes("加载失败"), missing.slice(0, 200));
  assert.equal(accountCards().length, 1);
});

// **回归**：app.js 里调了 `fmtTime()` 却没 import 它（它在 core.js）—— 任何一张
// **带到期时间**的服务授权都会让「我的账号」整页变成「加载失败：ReferenceError」。
// 不限期那条不会踩到，所以人工点一次很容易漏过去。
test("有到期时间的服务不能把整页搞崩", async () => {
  const got = await render(detail({ accounts: [ALI], services: DATED }));
  assert.ok(!got.includes("加载失败"), got.slice(0, 200));
  const [card] = serviceCards();
  assert.ok(card, `没渲染出服务卡：${got.slice(0, 400)}`);
  // 有期限的走另一条分支：「有效期」那栏写的是到期时间，不是那句长期
  const period = sectionOf(card, "有效期");
  assert.ok(period.includes("用到"), period);
  assert.ok(!period.includes("离职"), period);
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

// **回归**：personPage 里有两个早返回（「还没有权限快照」「没有云账号」）。
// 自建服务那一块要么排在它们之前、要么把它们的条件放宽（现在是后者：
// `!accounts.length && !pending.length && !services.length` 才返回）——
// 否则一个**没有云子账号**的人页面上什么都看不到，而这个功能的立项理由
// 恰恰是「最需要自建服务的人没有云子账号」。
//
// 服务并进云账号那一组之后，这条尤其要按**结构**判：那一组现在是
// `if (accounts.length || services.length)`，只漏掉 `|| services.length` 的话，
// 组不渲染、页面不报错、这个人一个字都看不到。
test("没有云账号的人也要看得到 —— 而这批人正是自建服务的目标人群", async () => {
  const got = await render(detail({ accounts: [], services: FOREVER }));
  assert.ok(!got.includes("加载失败"), got.slice(0, 200));
  const [card, ...rest] = serviceCards();
  assert.ok(card, `没有云账号时服务卡不见了：${got.slice(0, 400)}`);
  assert.equal(rest.length, 0);
  assert.equal(nameOf(card), "MLflow 实验跟踪");
  const group = groupOf(card);
  assert.ok(group, "卡得在那一组里，不能是散在外面的孤儿节点");
  assert.equal(accountCards().length, 0, "前提：这个人确实没有云账号");
  // 他不再走那个早返回，所以原先那张「先申请开一个子账号」的引导卡不会出现 ——
  // 而他恰恰最需要那句引导，组里得补上一条**点得动**的入口
  const apply = all(group).find((n) => n.tagName === "A" && n.getAttribute("href") === "#apply");
  assert.ok(apply, `没有云账号的人要有申请入口：${group.textContent}`);
  // **刻意不断言那张「先申请开一个子账号」的引导卡在不在**：只有服务、没有云账号的人
  // 该不该继续看到那句引导，是产品还在定的事（两种做法都说得通）。
  // 这条用例要保住的是「服务卡没有被早返回吃掉」，上面那几句已经证到了 ——
  // 多锁一句就会在引导卡回来的那天红，而红的原因和它要保护的性质无关
});

// **回归**：`expires_at_ts` 没有上界。`1e15` 能过后端全部校验，而
// `new Date(1e15 * 1000)` 超出 Date 量程 → `toISOString()` 抛 RangeError → 整页
// 变成「加载失败」。一张单子上的一个数字就能让这个人的整页打不开。
test("到期时间大到离谱也不能把整页搞崩", async () => {
  for (const ts of [1e15, Number.MAX_SAFE_INTEGER, -1e15]) {
    const got = await render(
      detail({ accounts: [ALI], services: [{ ...FOREVER[0], expires_at_ts: ts }] }),
    );
    assert.ok(!got.includes("加载失败"), `${ts}：${got.slice(0, 200)}`);
    const [card] = serviceCards();
    assert.ok(card, `${ts}：卡还得在`);
    assert.ok(sectionOf(card, "有效期"), `${ts}：有效期那一节还得在`);
  }
});

test("没有云账号、也没有服务授权时，还是那句「先申请开一个子账号」", async () => {
  const got = await render(detail({ accounts: [], services: [] }));
  assert.ok(got.includes("你目前没有阿里云或火山账号"), got.slice(0, 400));
  assert.equal(serviceCards().length, 0, got.slice(0, 400));
  assert.equal(accountCards().length, 0, got.slice(0, 400));
});
