// 取件页。**这一页是给外部合作方看的**，他们对这套面板没有任何熟悉度，
// 而且它常常是在会议室投屏、或者有人在旁边的时候打开的。
//
// 两条都只能在这一层锁：
//   · 敏感字段默认打码 —— 服务端返回的永远是明文，打不打码全在前端
//   · 复制拿到的必须是**真值**，不是屏幕上那串圆点。打码状态下复制到圆点的话，
//     人要到粘进终端、跑出一个签名错误时才发现，而那时他会以为凭证是坏的
import assert from "node:assert/strict";
import test from "node:test";
import "./dom.mjs";

globalThis.history = { replaceState() {} };
globalThis.location = { hash: "", pathname: "/" };

const { render, MASK } = await import("../../src/delivery/web/pickup.js");

const CRED = {
  access_key_id: "LTAI5tFAKEFAKEFAKE",
  access_key_secret: "sEcReT0123456789abcdefGHIJKLMNOP",
  expire: 1790000000,
  platform: "aliyun",
  region: "cn-hangzhou",
  endpoint: "oss-cn-hangzhou.aliyuncs.com",
  bucket_url: "wuji-bucket-hangzhou.oss-cn-hangzhou.aliyuncs.com",
  scope: "oss://wuji-bucket-hangzhou/batch/",
  caps: ["list", "download"],
  long_term: true,
};

function draw(over = {}) {
  render({ ...CRED, ...over });
  return document.getElementById("app");
}

//: 整页的可见文字。`JSON.stringify` 在这层 DOM 上会撞循环引用（parent 指回去）
function allText(n) {
  return (n.textContent || "") + (n.children || []).map(allText).join("");
}

function rows(app) {
  const out = [];
  const walk = (n) => {
    if (n.className && String(n.className).split(/\s+/).includes("cred-row")) out.push(n);
    (n.children || []).forEach(walk);
  };
  walk(app);
  return out;
}

function partsOf(row) {
  const label = row.children.find((c) => String(c.className).includes("cred-label"));
  const value = row.children.find((c) => String(c.className).includes("cred-value"));
  const tools = row.children.find((c) => String(c.className).includes("cred-tools"));
  return { label: label?.textContent, value, tools };
}

function byLabel(app, want) {
  const hit = rows(app).map(partsOf).find((p) => p.label === want);
  assert.ok(hit, `没有这一行：${want}`);
  return hit;
}

test("密钥默认是打码的，明文一个字都不在页面上", () => {
  const app = draw();
  const secret = byLabel(app, "AccessKeySecret");
  assert.equal(secret.value.textContent, MASK);
  assert.ok(String(secret.value.className).includes("masked"));
  assert.ok(!allText(app).includes(CRED.access_key_secret),
            "打码的时候明文不该出现在页面上的任何地方");
});

test("打码不按真实长度 —— 长度本身就能缩小暴力破解的范围", () => {
  const app = draw({ access_key_secret: "short" });
  assert.equal(byLabel(app, "AccessKeySecret").value.textContent, MASK);
});

test("AccessKeyId 不打码：它是标识不是密钥，人对着控制台核对用的就是它", () => {
  const app = draw();
  assert.equal(byLabel(app, "AccessKeyId").value.textContent, CRED.access_key_id);
});

test("点小眼睛才显示，再点一下收回去", () => {
  const app = draw();
  const { value, tools } = byLabel(app, "AccessKeySecret");
  const eye = tools.children.find((c) => String(c.className).includes("eye"));
  eye.dispatch("click");
  assert.equal(value.textContent, CRED.access_key_secret);
  assert.ok(!String(value.className).includes("masked"));
  eye.dispatch("click");
  assert.equal(value.textContent, MASK);
});

test("打码状态下复制拿到的是真值，不是那串圆点", async () => {
  let copied = null;
  // `navigator` 在 node 里是只读的，得用 defineProperty
  Object.defineProperty(globalThis, "navigator", {
    value: { clipboard: { writeText: async (t) => void (copied = t) } },
    configurable: true,
  });
  const app = draw();
  const { tools } = byLabel(app, "AccessKeySecret");
  const copy = tools.children.find((c) => !String(c.className).includes("eye"));
  await copy.dispatch("click");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(copied, CRED.access_key_secret);
});

test("SecurityToken 有才显示，而且同样默认打码", () => {
  assert.equal(rows(draw()).map(partsOf).filter((p) => p.label === "SecurityToken").length, 0);
  const app = draw({ security_token: "tok-abcdefg" });
  assert.equal(byLabel(app, "SecurityToken").value.textContent, MASK);
});

test("终端那段里的密钥也默认打码，但变量名留着", () => {
  const app = draw();
  const pre = [];
  const walk = (n) => {
    if (n.tagName === "PRE") pre.push(n);
    (n.children || []).forEach(walk);
  };
  walk(app);
  assert.equal(pre.length, 1);
  const text = pre[0].textContent;
  assert.ok(!text.includes(CRED.access_key_secret), "终端片段里不该有明文密钥");
  assert.ok(text.includes("ALIBABA_CLOUD_ACCESS_KEY_SECRET="), "变量名要留着");
  assert.ok(text.includes(CRED.access_key_id), "AccessKeyId 不是密钥，照常显示");
});
