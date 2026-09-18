// 申请表单的渲染：三种选项轴各自渲染成什么控件、填进去的值进了 payload 的哪个键。
//
// 跑法：make test-web  （或 node --test tests/web/*.test.mjs）
//
// 主用例跑的是**入库的** fixture（tests/web/fixture-templates.json），不是真实模板：
// identity/ 整个是 gitignored 的，而 catalog.load 对不存在的文件不报错、返回空 Catalog——
// 依赖它的话，新克隆的仓库里这几条会红，红得还没有提示。
// 真实模板另有一条用例兜着，文件在才跑：它防的是「模板里加了一种 fixture 里没有的轴」。
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "", pathname: "/" };

const { requestRoutes } = await import("../../src/delivery/web/requests.js");

const REPO = fileURLToPath(new URL("../..", import.meta.url));
const FIXTURE = "tests/web/fixture-templates.json";
const REAL = "identity/request-templates.json";

/** 把模板过一遍 Template.public()，拿到前端真正收到的那份 JSON。 */
function publicTemplates(file) {
  const out = execFileSync(
    "python3",
    [
      "-c",
      `import json, sys
from delivery import catalog
c = catalog.load(sys.argv[1])
print(json.dumps([t.public() for t in c.templates], ensure_ascii=False))`,
      file,
    ],
    { cwd: REPO, env: { ...process.env, PYTHONPATH: "src" }, encoding: "utf8" },
  );
  return JSON.parse(out);
}

function one(file, id) {
  const t = publicTemplates(file).find((x) => x.id === id);
  assert.ok(t, `${file} 里没有模板 ${id}`);
  return t;
}

function render(option) {
  const { applyForm } = requestRoutes({ load: () => null, errorView: () => null });
  const form = applyForm(option, [], () => {});
  const byId = new Map();
  for (const el of form.walk()) if (el.attrs && el.attrs.id) byId.set(el.attrs.id, el);
  const preview = [...form.walk()].find((e) => e.className === "preview-text");
  // 下拉监听的是 change、输入框监听的是 input。按控件类型派发，不要两种都发——
  // 都发的话，哪天有人把文本轴的监听写成 change，这里照绿、页面上却要点别处才刷新
  const sync = () => {
    for (const el of byId.values()) {
      if (el.tagName === "SELECT") el.dispatch("change");
      else el.dispatch("input");
    }
  };
  return { form, byId, preview, sync, payload: () => form.readPayload() };
}

/** 每一种轴都得渲染出它该有的控件。后端加了一种前端不认识的轴，这条当场红。 */
function assertEveryAxisRenders(o, where) {
  const { byId } = render(o);
  for (const axis of o.options) {
    const el = byId.get(`f-opt-${axis.id}`);
    assert.ok(el, `${where} 的「${axis.label}」没渲染出控件`);
    if (axis.text) {
      assert.equal(el.tagName, "INPUT", `${where}/${axis.id} 文本轴该是输入框`);
      assert.equal(el.attrs.type, "text");
      assert.equal(el.attrs.maxlength, String(axis.text.max));
    } else if (axis.number) {
      assert.equal(el.tagName, "INPUT", `${where}/${axis.id} 数字轴该是数字框`);
      assert.equal(el.attrs.type, "number");
    } else {
      assert.equal(el.tagName, "SELECT", `${where}/${axis.id} 选项轴该是下拉`);
      // 空下拉 = 前端把一种它不认识的轴当成了 choices 轴
      assert.ok(el.children.length > 0, `${where}/${axis.id} 渲染成了一个空下拉`);
      assert.equal(el.children.length, axis.choices.length);
    }
  }
}

test("每一种选项轴都渲染出了控件", () => {
  assertEveryAxisRenders(one(FIXTURE, "fixture-resource"), "fixture");
});

test("真实模板里的每一种轴也都渲染得出来（模板文件在才跑）", (t) => {
  if (!existsSync(new URL(`../../${REAL}`, import.meta.url))) {
    return t.skip(`${REAL} 不在（它是 gitignored 的），跳过`);
  }
  const all = publicTemplates(REAL).filter((x) => x.kind === "resource" && x.options.length);
  assert.ok(all.length, "真实模板里一个带选项轴的资源模板都没有");
  for (const o of all) assertEveryAxisRenders(o, o.id);
});

test("文本轴的值压成单行，进 texts 而不是 choices", () => {
  const { byId, preview, sync, payload } = render(one(FIXTURE, "fixture-resource"));
  // 换行是要挡的东西：留着就能在审批单上伪造一整行「AccessKey Secret：」
  byId.get("f-opt-project").value = "  舞肌 \n 推荐系统  ";
  sync();
  const p = payload();
  assert.equal(p.texts.project, "舞肌 推荐系统");
  assert.ok(!("project" in p.choices), "文本轴不该出现在 choices 里");
  assert.ok(!("project" in p.numbers), "文本轴不该出现在 numbers 里");
  assert.match(preview.textContent, /项目\/系统名称 舞肌 推荐系统/);
  assert.doesNotMatch(preview.textContent, /\n/);
});

test("三种轴各进各的键，没有哪个轴两边都进或都不进", () => {
  const o = one(FIXTURE, "fixture-resource");
  const { payload, sync } = render(o);
  sync();
  const p = payload();
  const seen = { ...p.choices, ...p.numbers, ...p.texts };
  const both = Object.keys(p.choices).filter((k) => k in p.numbers || k in p.texts);
  assert.deepEqual(both, [], "有轴同时进了两个键");
  // billing 被 net 的默认值（不要公网 IP）隐藏，其余都该出现
  for (const axis of o.options) {
    if (axis.id === "billing") continue;
    assert.ok(axis.id in seen, `轴 ${axis.id} 一个键都没进`);
  }
});

test("选项轴在预览里显示的是中文名，不是内部 id", () => {
  const { byId, preview, sync } = render(one(FIXTURE, "fixture-resource"));
  byId.get("f-opt-env").value = "prod";
  sync();
  assert.match(preview.textContent, /使用环境 生产/);
  assert.doesNotMatch(preview.textContent, /\bprod\b/);
});

test("被 hidden_when 隐藏的轴不进预览、也不进 payload", () => {
  const { byId, preview, sync, payload } = render(one(FIXTURE, "fixture-resource"));
  byId.get("f-opt-net").value = "none"; // 不要公网 IP → 不该再问计费方式
  sync();
  assert.doesNotMatch(preview.textContent, /公网计费/);
  assert.ok(!("billing" in payload().choices), "隐藏的轴不该进 payload");
  byId.get("f-opt-net").value = "m5";
  sync();
  assert.match(preview.textContent, /公网计费/);
  assert.ok("billing" in payload().choices);
});

test("占位下拉没选时，值是空串（垫片保真度）", () => {
  // 垫片把「首个 option」当成默认选中（真实浏览器就是这样），而不是「首个非空 value」。
  // 搞反的话「没选成本归属就该拦住提交」那条校验在测试里永远过、页面上却拦得住
  const { byId } = render(one(FIXTURE, "fixture-resource"));
  const cc = byId.get("f-cost");
  assert.ok(cc, "成本归属下拉没渲染出来");
  assert.equal(cc.value, "", "占位项该是默认选中，且它的 value 是空串");
});
