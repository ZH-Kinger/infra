// 体检的类别清单：后端加一类，前端那两张按类别写死的表必须跟上。
//
// 跑法：make test-web  （或 node --test tests/web/*.test.mjs）
//
// 为什么要有这条：`hygiene._SECTIONS` 是类别的唯一真相源，但前端 hygiene.js 里有两张
// 手写查找表跟着它走，而**没有任何东西强制它们同步**——漏了也不会报错：
//   · TONE 缺一项  → `class="pill "`，徽章没颜色。看起来像样式没加载，不像少了一类。
//   · SHORT 缺一项 → `shortTitle()` 回退成完整长标题（「人已经不在通讯录里，云账号还在」），
//                    塞进统计条里会把那一行撑变形。
// 两种都是「页面还能打开、只是不对」，最容易一路带上线。stray_bucket 这次就差点漏掉。
//
// 后端那份清单**问 Python 要**（和 apply-form 那条用例同款做法），不拿正则抠：
// 正则会随 _SECTIONS 的排版变（单行元组 / 多行元组混着写），失配的表现是「抠出一半」，
// 而抠出一半的守卫看起来仍然是绿的。前端那两张表是 module 私有 const、导不出来，
// 只能正则读源文件 —— 那边抠出 0 个键时当场失败，别让它退化成空断言。
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

const REPO = fileURLToPath(new URL("../..", import.meta.url));
const JS = readFileSync(`${REPO}src/delivery/web/hygiene.js`, "utf8");

/** 后端 `_SECTIONS` 里的 kind，顺序照原样（也是页面的展示顺序）。 */
function backendKinds() {
  const out = execFileSync(
    "python3",
    [
      "-c",
      `import json
from delivery import hygiene
print(json.dumps([k for k, _t, _n in hygiene._SECTIONS]))`,
    ],
    { cwd: REPO, env: { ...process.env, PYTHONPATH: "src" }, encoding: "utf8" },
  );
  const kinds = JSON.parse(out);
  assert.ok(kinds.length >= 5, `_SECTIONS 只有 ${kinds.length} 个 kind，读错了`);
  return kinds;
}

/** 前端某张查找表的键。 */
function tableKeys(name) {
  const block = JS.match(new RegExp(`const ${name} = \\{([^}]*)\\}`));
  assert.ok(block, `在 web/hygiene.js 里找不到 ${name} 这张表`);
  const keys = [...block[1].matchAll(/([A-Za-z_]+)\s*:/g)].map((m) => m[1]);
  assert.ok(keys.length > 0, `${name} 抠出 0 个键，正则多半失配了`);
  return keys;
}

test("后端每一类在前端两张表里都有条目", () => {
  const kinds = backendKinds();
  assert.ok(kinds.includes("stray_bucket"), "样本检查：_SECTIONS 里应当有 stray_bucket");
  for (const table of ["TONE", "SHORT"]) {
    const keys = new Set(tableKeys(table));
    const missing = kinds.filter((k) => !keys.has(k));
    assert.deepEqual(
      missing,
      [],
      `后端 hygiene._SECTIONS 新增了类别 [${missing.join(", ")}]，` +
        `前端 web/hygiene.js 的 ${table} 表没跟上。` +
        (table === "TONE"
          ? "结果是这一类的徽章没有颜色（class 里是个空串），看起来像样式坏了。"
          : "结果是统计条上印完整长标题，把那一行撑变形。") +
        " 两张表都要补。",
    );
  }
});

test("前端两张表里没有后端已经不存在的类别", () => {
  const kinds = new Set(backendKinds());
  for (const table of ["TONE", "SHORT"]) {
    const stale = tableKeys(table).filter((k) => !kinds.has(k));
    assert.deepEqual(
      stale,
      [],
      `web/hygiene.js 的 ${table} 表里还留着 [${stale.join(", ")}]，` +
        "但后端 hygiene._SECTIONS 里已经没有这一类了。" +
        "留着不会报错，但下一个人会照着它猜前端支持哪些类别，猜错的那次没人会发现。",
    );
  }
});

test("两张表的键完全一致", () => {
  // 只补一张的话：徽章有颜色、标题却是长的（或反过来），而两者在页面上离得很远，
  // 一眼看不出是同一个原因
  assert.deepEqual(new Set(tableKeys("TONE")), new Set(tableKeys("SHORT")));
});
