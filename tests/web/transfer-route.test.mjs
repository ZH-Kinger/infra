// 迁移申请页的「走哪条链」结论。
//
// 这一层的价值全在**提交之前**：填完两个路径就该知道这单办不办得成。
// 原先这里把六条链都画了标签和路径图 —— 预热、沉降、PFS 直传全在 ——
// 而面板只接了两条。画得出来却办不成，人会填完、提交、等审批，
// 最后被一句「桶不在可申请的范围里」挡下来，那句话指不到真正的原因。
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

const { routeOf, parseUri } = await import("../../src/delivery/web/storage.js");

const BUCKETS = [
  { name: "wuji-bucket-hangzhou", region: "cn-hangzhou" },
  { name: "wuji-bangkok", region: "ap-southeast-7" },
  { name: "data-tran", region: "cn-shanghai" },
];

const route = (a, b) => routeOf(parseUri(a), parseUri(b), BUCKETS);

test("同云搬运有结论", () => {
  const got = route("oss://wuji-bucket-hangzhou/a/", "oss://wuji-bangkok/b/");
  assert.ok(!got.error, got.error);
  assert.ok(got.chain);
});

test("跨地域不许承诺后端没做的事", () => {
  // 这里以前画的是「源（杭州）→ wuji-sing（新加坡）→ 目的」，还写着「副本 30 天后自动清理」。
  // 两句都不是真的：后端走在线迁移直连，没有中转那一跳；全仓库也没有任何 30 天清理。
  // 而 2026-09-21 实测曼谷 5 个一级目录杭州一个都没有 ——
  // 那句话真去实现就是删掉只此一份的数据。申请人是照这些话决定要不要提单的。
  const got = route("oss://wuji-bucket-hangzhou/a/", "oss://wuji-bangkok/b/");
  const text = `${got.label || ""}${got.note || ""}${got.warn || ""}`;
  assert.doesNotMatch(text, /30 ?天/, "承诺了一个不存在的自动清理");
  // 只拦具体的假承诺。「不走中转」是对的，不能因为里面有「中转」两个字就判失败 ——
  // 断言写得太宽，逼的是把话说得含糊，而不是说得准确
  assert.doesNotMatch(text, /wuji-sing|新加坡/, "画了一条后端不走的链路");
  assert.equal(got.hops.length, 2, "直连就是两跳，别画出第三跳");
  assert.match(text, /不会自动清理/, "要说清楚搬过去的那份归谁管");
});

test("跨云两个方向都有结论", () => {
  const up = route("oss://wuji-bucket-hangzhou/a/", "tos://data-tran/b/");
  assert.ok(!up.error, up.error);
  assert.match(up.label, /跨云/);
  // 火山侧只能指定目标桶 —— 这句必须出现在提交之前，不然人会以为数据落在他填的目录里
  assert.match(up.warn || "", /指不了目标目录|目标桶/);

  const down = route("tos://data-tran/a/", "oss://wuji-bucket-hangzhou/b/");
  assert.ok(!down.error, down.error);
  assert.match(down.label, /跨云/);
});

test("预热和沉降走同一个表单，方向由地址推出来", () => {
  // **不让人选「这是预热还是沉降」** —— 那是系统能自己看出来的事，
  // 而人选错的表现是把数据往相反方向覆盖一遍，不可逆。
  for (const [from, to, want] of [
    ["oss://wuji-bucket-hangzhou/a/", "cpfs://bmcpfs-1/b/", /预热/],
    ["cpfs://bmcpfs-1/a/", "oss://wuji-bucket-hangzhou/b/", /沉降/],
    ["tos://data-tran/a/", "vepfs://vepfs-1/b/", /预热/],
    ["vepfs://vepfs-1/a/", "tos://data-tran/b/", /沉降/],
  ]) {
    const got = route(from, to);
    assert.ok(!got.error, `${from} → ${to}：${got.error}`);
    assert.equal(got.chain, "dataflow", `${from} → ${to}`);
    assert.match(got.label, want, `${from} → ${to}`);
  }
});

test("CPFS 那两条要提前说「面板不会替你建绑定」", () => {
  // 阿里那边对已有数据的目录建数据流动会把它**清空**，所以面板只复用现成绑定。
  // 不提前说的话，人填完提交才被拒，而那句话看起来像权限问题。
  const got = route("oss://wuji-bucket-hangzhou/a/", "cpfs://bmcpfs-1/b/");
  assert.match(got.warn, /绑定/);
  assert.match(got.warn, /不会替你建/);
});

test("两个并行文件系统之间说清楚是「做不了」不是「还没做」", () => {
  for (const [from, to] of [
    ["vepfs://vepfs-1/a/", "cpfs://bmcpfs-1/b/"],
    ["cpfs://bmcpfs-1/a/", "vepfs://vepfs-1/b/"],
    ["cpfs://bmcpfs-1/a/", "cpfs://bmcpfs-2/b/"],
  ]) {
    const got = route(from, to);
    assert.ok(got.error, `${from} → ${to} 不该给出一条办不成的链`);
    assert.match(got.error, /没有直连/, `${from} → ${to}`);
    assert.ok(!got.chain, `${from} → ${to} 不该有 chain`);
  }
});

test("源和目标相同仍然拦得住", () => {
  assert.match(route("oss://wuji-bucket-hangzhou/a/", "oss://wuji-bucket-hangzhou/a/").error,
    /同一个地方/);
});
