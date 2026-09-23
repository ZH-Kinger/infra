// 待办页的接线：路由、导航标签、角标、静态资源、管理员落地页。
//
// 跑法：node --test tests/web/*.test.mjs
//
// 为什么要在源码层面钉这些：这一页的接线错了**不会报错**，只会让功能悄悄不存在。
//   · 角标的选择器指向一个 index.html 里没有的 data-tab → querySelectorAll 返回空，
//     角标永远不出现。而「今天没有待办」和「角标挂了」在页面上是同一个样子。
//   · parseHash 里漏了 admin/todo → 点导航掉回「我的云账号」，看起来像没登录成管理员。
//   · server._STATIC 里漏了 /todo.js → 整页白屏（模块加载失败），而其余页面都正常，
//     没人会想到是后端少了一行静态路由。
// 这几处分散在三个文件里，谁都不强制别人跟上，所以只能从外面拴一道。
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

const REPO = fileURLToPath(new URL("../..", import.meta.url));
const APP = readFileSync(`${REPO}src/delivery/web/app.js`, "utf8");
const HTML = readFileSync(`${REPO}src/delivery/web/index.html`, "utf8");

const TAB = "admin-todo";
const HASH = "#admin/todo";

test("导航里有「待办」这个标签，href 指向 #admin/todo", () => {
  const row = HTML.split("\n").find((l) => l.includes(`data-tab="${TAB}"`));
  assert.ok(row, "index.html 的管理后台导航里没有 admin-todo 标签");
  assert.match(row, new RegExp(`href="${HASH}"`));
  // 它该排在最前面：待办页是管理员的落地页，排在第五个等于又埋了一层
  const admin = HTML.slice(HTML.indexOf('id="tabs-admin"'));
  const tabs = [...admin.matchAll(/data-tab="([a-z-]+)"/g)].map((m) => m[1]);
  assert.equal(tabs[0], TAB, `待办应当是管理后台第一个标签，实际顺序：${tabs.join(", ")}`);
});

test("角标挂在确实存在的那个标签上", () => {
  // 这是「角标搬家」最容易留下的半截活：选择器改了、导航没加，
  // 或者导航加了、选择器还指着旧的 admin-iam
  const sel = APP.match(/querySelectorAll\('\.tab\[data-tab="([a-z-]+)"\]'\)/);
  assert.ok(sel, "app.js 里找不到角标的标签选择器，正则多半失配了");
  assert.equal(sel[1], TAB);
  assert.ok(HTML.includes(`data-tab="${sel[1]}"`), `角标指向的 ${sel[1]} 在导航里不存在`);
});

test("角标读的是新的 counts 键，不是写死的 0", () => {
  // urgent / total 这两个键由后端 Backend.admin_todo 给，和待办页同一份计算。
  // 改名而不同步的表现是角标恒为 0 —— 看起来就是「今天没事」
  assert.match(APP, /todo\.urgent/);
  assert.match(APP, /todo\.total/);
});

test("路由认得 admin/todo", () => {
  assert.match(APP, /path === "admin\/todo"/, "parseHash 里没有 admin/todo 这条");
  assert.match(APP, /page === "admin-todo"/, "route 里没有 admin-todo 这条");
  assert.match(APP, /renderTodo/);
});

test("管理员从员工视图切过去落在待办页", () => {
  // 落地页还指着「申请与开通」的话，这一页就又回到了「要自己想起来点进去」的状态,
  // 而它存在的全部理由就是不用想起来
  const line = APP.split("\n").find((l) => l.includes("space-switch") || l.includes('"#me"'));
  assert.ok(line, "找不到空间切换那行");
  assert.match(APP, new RegExp(`space === "admin" \\? "#me" : "${HASH}"`));
});

test("后端把 /todo.js 当静态资源发出去", () => {
  // CSP 只允许同源脚本，而 _STATIC 是唯一的白名单。漏了这一行的表现是整页白屏
  const out = execFileSync(
    "python3",
    [
      "-c",
      `import json
from delivery import server
print(json.dumps(sorted(server._STATIC)))`,
    ],
    { cwd: REPO, env: { ...process.env, PYTHONPATH: "src" }, encoding: "utf8" },
  );
  const paths = JSON.parse(out);
  assert.ok(paths.length > 5, `_STATIC 只有 ${paths.length} 条，读错了`);
  assert.ok(paths.includes("/todo.js"), `server._STATIC 里没有 /todo.js：${paths.join(", ")}`);
});

test("app.js 引的每个页面模块都在静态白名单里", () => {
  // 反向锁：下一个人加页面时，这条会替他把「后端忘了放行」这一步喊出来
  const imported = [...APP.matchAll(/from "\.\/([a-z]+)\.js"/g)].map((m) => `/${m[1]}.js`);
  const out = execFileSync(
    "python3",
    [
      "-c",
      `import json
from delivery import server
print(json.dumps(sorted(server._STATIC)))`,
    ],
    { cwd: REPO, env: { ...process.env, PYTHONPATH: "src" }, encoding: "utf8" },
  );
  const allowed = new Set(JSON.parse(out));
  // core.js 由别的模块间接引入，同样要放行
  const missing = [...new Set(imported)].filter((p) => !allowed.has(p));
  assert.deepEqual(missing, [], `app.js 引了这些模块，但后端 _STATIC 没放行：${missing.join(", ")}`);
});
