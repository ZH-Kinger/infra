// 「你的访问密钥」卡：**「没采到」绝不能渲染成「你没有密钥」**。
//
// 这一条后端分得很干净（keys / uncollected / accounts 三个字段，还有单测钉着），
// 可最后一米差点丢了：`keys.length ? 表格 : "你名下没有长期访问密钥 —— 这是好事"`
// 这一行，在「全部没采到」时会给全公司发一句假的安心话。
//
// 这类 bug 服务端用例一条都抓不到（后端返回是对的），只能在这一层锁。审计就是这么逮到的。
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "", pathname: "/" };

const { assetsPage } = await import("../../src/delivery/web/assets.js");

const ACC = "1000000000000001";
const WHERE = { platform: "aliyun", account: ACC, account_label: "阿里云主账号", user: "lisi" };

function key(over = {}) {
  return {
    ...WHERE,
    id: "LTAI0001",
    status: "Active",
    active: true,
    created: "2024-01-01T00:00:00Z",
    age_days: 400,
    last_used: "",
    idle_days: null,
    never_used: true,
    flags: [],
    ...over,
  };
}

/** 渲染员工资产页，返回整页的纯文本 + 密钥卡那一节。 */
function render(keysBlock, { accounts = [] } = {}) {
  const nodes = assetsPage(
    { captured_at: "2026-09-18T00:00:00+08:00", accounts, holdings: [], keys: keysBlock },
    false,
  );
  const cards = nodes.filter((n) => (n.className || "").includes("asset-card"));
  const text = nodes.map((n) => n.textContent).join("\n");
  return { text, card: cards[cards.length - 1], cards };
}

const BASE = { captured_at: "2026-09-18T00:00:00+08:00", stale_days: 180, unused_days: 90 };

test("采到了：列出每把密钥的年龄和最近使用", () => {
  const { text } = render({ ...BASE, accounts: 1, keys: [key({ idle_days: 3, never_used: false })], uncollected: [] });
  assert.match(text, /LTAI0001/);
  assert.match(text, /400 天|1 年/);
  assert.match(text, /3 天前/);
});

test("确实一把都没有：说出来，并且说清这是好事", () => {
  // 只看密钥卡本身：整页文本里还有资产那一段的「还没采到你的云资源」，说的不是一回事
  const { card } = render({ ...BASE, accounts: 1, keys: [], uncollected: [] });
  assert.match(card.textContent, /没有长期访问密钥/);
  assert.doesNotMatch(card.textContent, /没采到/);
  assert.match(card.textContent, /0 把/);
});

test("一把都没采到：绝不能说成「你没有密钥」", () => {
  // 采集身份掉了 ram:ListAccessKeys 就是这个形状。全员同时看到，所以这条最要命。
  const { text, card } = render({ ...BASE, accounts: 1, keys: [], uncollected: [{ ...WHERE }] });
  assert.doesNotMatch(text, /没有长期访问密钥/, "「没采到」被渲染成了「你没有密钥」");
  assert.match(text, /没采到/);
  assert.match(text, /不是.*说你没有密钥/, "要明说这不等于没有");
  // 头上那个数字同样不能写 0 —— 写 0 就是替云上回答了一个这次根本没问到的问题
  assert.doesNotMatch(card.textContent, /\b0 把/, "没采到时不该显示「0 把」");
  assert.match(card.textContent, /\? 把/);
});

test("一半采到一半没采到：表格照出，但要说明这份不完整", () => {
  const { text } = render({
    ...BASE,
    accounts: 2,
    keys: [key()],
    uncollected: [{ ...WHERE, user: "lisi2" }],
  });
  assert.match(text, /LTAI0001/);
  assert.match(text, /不完整/);
  assert.match(text, /lisi2/);
});

test("压根没有云账号的人：整张卡都不出现", () => {
  const { cards } = render({ ...BASE, accounts: 0, keys: [], uncollected: [] });
  assert.equal(cards.length, 0, "没有云账号还给他一张空密钥卡是噪音");
});

test("后端没返回 keys 这一段时不渲染，也不误报", () => {
  const { text, cards } = render({});
  assert.equal(cards.length, 0);
  assert.doesNotMatch(text, /没有长期访问密钥/);
});

test("卡上必须标出快照时间", () => {
  // 年龄和「多久没用」是拿此刻减快照里的时间算的。快照放旧了，一把昨天还在用的密钥
  // 会显示成「15 天前」，再配上「没人用，停掉吧」的建议 —— 有人真会去停一把在跑的
  const { card } = render({ ...BASE, accounts: 1, keys: [key()], uncollected: [] });
  assert.match(card.textContent, /权限快照/);
});

test("已停用的密钥照列，但不催人轮换", () => {
  const { card } = render({
    ...BASE,
    accounts: 1,
    keys: [key({ id: "DEAD0000", active: false, status: "Inactive", flags: [] })],
    uncollected: [],
  });
  assert.match(card.textContent, /DEAD0000/);
  assert.match(card.textContent, /已停用/);
  assert.doesNotMatch(card.textContent, /该换了/);
});

test("有密钥却没采到资产时，不能反过来说「你还没有云账号」", () => {
  // 资产采集和权限采集是两套独立的，前者没跑是常态。密钥本身就是账号存在的证据，
  // 紧跟着再说一句「你还没有云账号」是自相矛盾，看的人只会不信这一页
  const { text } = render({ ...BASE, accounts: 1, keys: [key()], uncollected: [] });
  assert.doesNotMatch(text, /你还没有云账号/);
  assert.match(text, /还没采到你的云资源/);
});


// ── 数据集卡 ────────────────────────────────────────────────────────────
// 数据集是**唯一一类归属自带的资产**（UserId 就是属主），所以员工那页一上来就有内容。
// 而「没采到」和「你没有数据集」必须分得开 —— 和密钥台账同一条规矩。
function ds(over = {}) {
  return {
    name: "wangzihan", region: "cn-hangzhou", workspace: "590221",
    workspace_name: "ai_hz_gpu", source: "BMCPFS", path: "/wangzihan",
    uri: "bmcpfs://x/wangzihan/", accessibility: "ROLE_PUBLIC",
    owner_login: "wangzihan", owner_name: "王梓涵", owner_kind: "user",
    owner_deleted_at: "", account_label: "阿里云主账号", ...over,
  };
}

function page(datasets, admin = false) {
  const nodes = assetsPage(
    { captured_at: "2026-09-18T00:00:00+08:00", accounts: [], holdings: [], keys: {}, datasets },
    admin,
  );
  return nodes.map((n) => n.textContent).join("\n");
}

test("数据集出现在员工资产页上", () => {
  const text = page({ collected: true, items: [ds()], abandoned: 0 });
  assert.match(text, /你的数据集/);
  assert.match(text, /wangzihan/);
  assert.match(text, /CPFS/);
});

test("没采到时整张卡不出现，也不说「你没有数据集」", () => {
  // collected:false = 没跑过采集 / PAI 权限掉了。这跟「确实没有」是两回事
  const text = page({ collected: false, items: [], abandoned: 0 });
  assert.doesNotMatch(text, /你的数据集/);
  assert.doesNotMatch(text, /还没有数据集/);
});

test("PUBLIC 要明说「谁都能删」，不能只印枚举值", () => {
  // 组里那条 pai:* 策略对 PUBLIC 无条件放行，包括删除。印个 "PUBLIC" 没人看得懂风险
  const text = page({ collected: true, items: [ds({ accessibility: "PUBLIC" })], abandoned: 0 });
  assert.match(text, /谁都能删/);
  // 徽章走的是 createTextNode，**星号会原样显示** —— 本仓库没有 Markdown 渲染。
  // 不断言这条的话，`**谁都能删**` 和 `谁都能删` 两种写法都会绿
  assert.doesNotMatch(text, /\*\*/, "徽章里不该出现 Markdown 星号");
  // 为什么谁都能删，得在卡片说明里讲清楚 —— 塞不进小标签
  assert.match(text, /无条件放行/);
});

test("管理员那边多一列属主，还要提示有几条是被遗弃的", () => {
  const text = page(
    { collected: true, items: [ds({ owner_kind: "gone", owner_name: "朱俊磊" })], abandoned: 1 },
    true,
  );
  assert.match(text, /PAI 数据集/);
  assert.match(text, /朱俊磊/);
  assert.match(text, /号已删/);
  assert.match(text, /1 条的属主/);
});

test("员工看不到别人遗弃的计数", () => {
  const text = page({ collected: true, items: [ds()], abandoned: 0 }, false);
  assert.doesNotMatch(text, /属主 RAM 号已经删/);
});
