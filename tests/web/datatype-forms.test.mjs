// 数据类型词表驱动的两张表单：「新建数据目录」按词表逐层问，「新增数据类型」挡住保留名。
//
// 这一层要单独锁：后端返回的是对的（层级、类型名都在 public() 里），但表单要是
// 还按写死的老分类渲染，词表加了新类型申请页上也选不到它 —— 而没有任何报错。
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

globalThis.location = { hash: "", pathname: "/" };
const { directoryFields, datatypeFields, splitLayers } = await import("../../src/delivery/web/storage.js");

const field = (id, label, control) => control;
const TYPES = {
  label: { label: "标注成品", layers: ["来源", "版本", "批次ID"] },
  "public-datasets": { label: "开源数据集", layers: ["数据集名"] },
};
const DIR = {
  kind: "storage",
  buckets: [
    { name: "wuji-processed-hz", region: "cn-hangzhou", stages: ["label"] },
    { name: "wuji-bucket-hangzhou", region: "cn-hangzhou", stages: ["public-datasets"] },
  ],
  types: TYPES,
};

function walk(n, want, out = []) {
  if (want(n)) out.push(n);
  for (const c of n.children || []) walk(c, want, out);
  return out;
}

test("词表类型按登记的层级逐层问，最后一层是批次", () => {
  const got = directoryFields(DIR, { field, update() {} });
  const root = { children: got.fields };
  const segs = walk(root, (n) => String(n.attrs?.id || "").startsWith("f-seg-"));
  assert.deepEqual(segs.map((x) => x.attrs.placeholder), ["来源", "版本"], "中间两层各一个输入框");
  segs[0].value = "shutu";
  segs[1].value = "v2";
  const scene = walk(root, (n) => n.attrs?.id === "f-scene")[0];
  scene.value = "kitchen";
  const out = got.read.segments();
  assert.equal(out.length, 3);
  assert.deepEqual(out.slice(0, 2), ["shutu", "v2"]);
  assert.match(out[2], /^\d{8}-kitchen$/, "批次 ID 不再重复一遍来源 —— 来源已经是单独一层目录");
});

test("中间某层没填或带斜杠，提交前就拦住", () => {
  const got = directoryFields(DIR, { field, update() {} });
  const root = { children: got.fields };
  const segs = walk(root, (n) => String(n.attrs?.id || "").startsWith("f-seg-"));
  segs[0].value = "a/b";
  segs[1].value = "v2";
  const errs = got.checks.map((c) => c()).filter(Boolean);
  assert.ok(errs.length, "a/b 能让路径跳出它那一层，必须拦");
});

test("新增类型：保留名、已有的名字都拦住", () => {
  const got = datatypeFields({ kind: "datatype", buckets: [{ name: "b1", region: "r" }], types: TYPES },
    { field, update() {} });
  const root = { children: got.fields };
  const key = walk(root, (n) => n.attrs?.id === "f-dt-key")[0];
  for (const bad of ["tmp", "general", "label"]) {
    key.value = bad;
    const err = got.checks[0]();
    assert.ok(err, `${bad} 应该被拦`);
  }
  key.value = "sim-data";
  assert.equal(got.checks[0](), "");
});

test("层级可以用空格、逗号或斜杠分开", () => {
  assert.deepEqual(splitLayers("来源, 批次ID"), ["来源", "批次ID"]);
  assert.deepEqual(splitLayers("来源/版本/批次ID"), ["来源", "版本", "批次ID"]);
  assert.deepEqual(splitLayers("  "), []);
});
