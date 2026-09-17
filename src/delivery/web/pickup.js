// 查看凭证。这一页在登录之外 —— 凭证常发给外部合作方，他们没有面板账号。
//
// 地址是 /c/<申请单号>#<密钥>。密钥在 # 之后：浏览器不会把 fragment 发给服务端，
// 所以它不进访问日志、不进 Referer，只在使用方自己的浏览器里。
//
// **不抹地址栏**：这一页是可以反复打开的（换机器、重装、同事接手），抹掉就刷新不了。
// 每次打开服务端都会在申请单里记一笔。

const app = document.getElementById("app");

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function show(...nodes) {
  app.replaceChildren(...nodes);
}

function card(title, ...body) {
  return h("div", { class: "card" }, h("div", { class: "group-label" }, title), ...body);
}

function fail(message) {
  show(h("div", { class: "card empty" }, h("h2", {}, "打不开"), h("p", {}, message)));
}

function row(label, value, copyable) {
  return h(
    "div",
    { class: "perm" },
    h("span", { class: "muted" }, label),
    h("code", { class: "secret-value" }, value),
    copyable
      ? h(
          "button",
          {
            type: "button",
            class: "btn ghost small",
            onclick: async (e) => {
              try {
                await navigator.clipboard.writeText(value);
                e.currentTarget.textContent = "已复制";
              } catch {
                e.currentTarget.textContent = "复制失败，请手动选中";
              }
            },
          },
          "复制",
        )
      : null,
  );
}

function render(c) {
  const rows = [
    ["AccessKeyId", c.access_key_id],
    ["AccessKeySecret", c.access_key_secret],
  ];
  if (c.security_token) rows.push(["SecurityToken", c.security_token]);
  const until = new Date(c.expire * 1000).toLocaleString("zh-CN", { hour12: false });
  const env =
    c.platform === "volcano"
      ? `export VOLCENGINE_ACCESS_KEY=${c.access_key_id}\nexport VOLCENGINE_SECRET_KEY=${c.access_key_secret}`
      : [
          `export ALIBABA_CLOUD_ACCESS_KEY_ID=${c.access_key_id}`,
          `export ALIBABA_CLOUD_ACCESS_KEY_SECRET=${c.access_key_secret}`,
          c.security_token ? `export ALIBABA_CLOUD_SECURITY_TOKEN=${c.security_token}` : "",
          `export ALIBABA_CLOUD_REGION_ID=${c.region}`,
          "export ALIBABA_CLOUD_IGNORE_PROFILE=TRUE",
        ]
          .filter(Boolean)
          .join("\n");

  show(
    h(
      "header",
      { class: "masthead" },
      h("h1", {}, "凭证"),
      h("p", { class: "lede" }, "这个链接可以反复打开。每次打开都会记录在申请单里。"),
    ),
    card(
      "密钥",
      ...rows.map(([k, v]) => row(k, v, true)),
      h("div", { class: "opt-meta pad-body" }, `${until} 到期`),
    ),
    card(
      "连接信息",
      row("权限", (c.caps || []).join("、"), false),
      row("范围", c.scope, false),
      row("地域", c.region, false),
      row("外网 Endpoint", c.endpoint, false),
      row("桶域名", c.bucket_url, false),
    ),
    card(
      "在终端里用",
      h("pre", { class: "snippet-body pad-block" }, env),
      c.platform === "volcano"
        ? h(
            "ul",
            { class: "opt-notes pad-notes" },
            h("li", {}, "火山 CLI 的环境变量优先级最低：你机器上只要存在 profile，上面这些会被忽略。"),
            h("li", {}, "tosutil 完全不读环境变量，要写配置文件，写完记得 chmod 600。"),
          )
        : null,
    ),
    h(
      "p",
      { class: "lede pad-foot" },
      (c.long_term
        ? "有效期写在策略里，服务端每次调用按当前时间判定，到期即失效。到期后子账号和密钥会被删除。"
        : "这是临时凭证，到点自动失效，云上不留任何东西。") +
        "请勿转发这个链接：拿到链接就等于拿到凭证。",
    ),
  );
}

async function main() {
  const key = location.hash.slice(1);
  const id = decodeURIComponent(location.pathname.replace(/^\/c\//, ""));
  if (!key || !id) return fail("这个地址不完整。请用审批评论里给的完整链接打开。");

  show(h("div", { class: "card empty" }, h("p", {}, "正在解密…")));
  let resp;
  try {
    resp = await fetch("/api/pickup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, key }),
      cache: "no-store",
    });
  } catch {
    return fail("连不上服务器。检查网络后刷新重试。");
  }
  let data = {};
  try {
    data = await resp.json();
  } catch {
    /* 下面按状态码处理 */
  }
  if (!resp.ok) return fail(data.error || `打不开（${resp.status}）`);
  if (!data.credential) return fail("服务端没有返回凭证，请联系管理员。");
  render(data.credential);
}

main();
