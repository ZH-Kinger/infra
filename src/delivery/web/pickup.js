// 查看凭证。这一页在登录之外 —— 凭证常发给外部合作方，他们没有面板账号。
//
// 地址是 /c/<申请单号>#<密钥>。密钥在 # 之后：浏览器不会把 fragment 发给服务端，
// 所以它不进访问日志、不进 Referer，只在使用方自己的浏览器里。
//
// **不抹地址栏**：这一页是可以反复打开的（换机器、重装、同事接手），抹掉就刷新不了。
// 每次打开服务端都会在申请单里记一笔。

//: **渲染时才找容器**，不在模块加载那一刻找 —— 脚本是 `type="module"`（defer），
//: 正常情况下 DOM 已经在了，但拿一次就存下来的话，任何「加载顺序变了」
//: 都会变成一个静默的 null，而表现是整页空白、控制台一行 replaceChildren of null

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
  document.getElementById("app").replaceChildren(...nodes);
}

function card(title, ...body) {
  return h("div", { class: "card" }, h("div", { class: "group-label" }, title), ...body);
}

function fail(message) {
  show(h("div", { class: "card empty" }, h("h2", {}, "打不开"), h("p", {}, message)));
}

//: 打码用的字符。**不按真实长度打**（`"•".repeat(value.length)` 会把密钥长度
//: 泄露给肩后看屏幕的人，而长度本身就能缩小暴力破解的范围）。固定一串就够了。
const MASK = "••••••••••••••••••••••••";

function copyButton(getText, { label = "复制" } = {}) {
  //: **每次点都重新取值**（`getText` 是函数不是字符串）—— 打码状态下复制的
  //: 也必须是真值，否则人复制到的是一串圆点，而那要到粘进终端才发现。
  return h(
    "button",
    {
      type: "button",
      class: "icon-btn",
      title: "复制",
      "aria-label": "复制",
      onclick: async (e) => {
        const btn = e.currentTarget;
        const was = btn.textContent;
        try {
          await navigator.clipboard.writeText(getText());
          btn.textContent = "已复制";
          btn.classList.add("ok");
        } catch {
          //: 非 https 或者旧浏览器会没有 clipboard —— 说清楚要手动选，
          //: 不要只变成一个没反应的按钮
          btn.textContent = "手动选中复制";
          btn.classList.add("bad");
        }
        setTimeout(() => {
          btn.textContent = was;
          btn.classList.remove("ok", "bad");
        }, 1600);
      },
    },
    label,
  );
}

function eyeButton(onToggle) {
  let shown = false;
  const btn = h(
    "button",
    {
      type: "button",
      class: "icon-btn eye",
      title: "显示",
      "aria-label": "显示",
      "aria-pressed": "false",
      onclick: () => {
        shown = !shown;
        btn.textContent = shown ? "🙈" : "👁";
        btn.title = btn.ariaLabel = shown ? "隐藏" : "显示";
        btn.setAttribute("aria-pressed", String(shown));
        onToggle(shown);
      },
    },
    "👁",
  );
  return btn;
}

function row(label, value, { secret = false, copyable = false } = {}) {
  const text = String(value ?? "");
  const box = h("code", { class: "cred-value" + (secret ? " masked" : "") },
                secret ? MASK : text);
  const tools = h("span", { class: "cred-tools" });
  if (secret) {
    tools.append(
      eyeButton((shown) => {
        box.textContent = shown ? text : MASK;
        box.classList.toggle("masked", !shown);
      }),
    );
  }
  if (copyable) tools.append(copyButton(() => text));
  return h("div", { class: "cred-row" }, h("span", { class: "cred-label" }, label), box, tools);
}

function render(c) {
  //: AccessKeyId 不打码：它是标识不是密钥，而且人对着控制台核对用的就是它。
  //: Secret 和 SecurityToken 默认打码 —— 这一页常常是在会议室投屏上打开的。
  const rows = [
    ["AccessKeyId", c.access_key_id, false],
    ["AccessKeySecret", c.access_key_secret, true],
  ];
  if (c.security_token) rows.push(["SecurityToken", c.security_token, true]);
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
  //: 终端那段里嵌着 secret，所以它也默认打码 —— 只把密钥那几行的值换掉，
  //: 变量名留着，人一眼能看出这段是干什么的
  const envMasked = env.replace(
    /(SECRET_KEY|ACCESS_KEY_SECRET|SECURITY_TOKEN)=.*/g,
    (_m, k) => `${k}=${MASK}`,
  );
  const pre = h("pre", { class: "snippet-body pad-block" }, envMasked);

  show(
    h(
      "header",
      { class: "masthead" },
      h("h1", {}, "凭证"),
      h("p", { class: "lede" }, "这个链接可以反复打开。每次打开都会记录在申请单里。"),
    ),
    card(
      "密钥",
      ...rows.map(([k, v, secret]) => row(k, v, { secret, copyable: true })),
      h("div", { class: "opt-meta pad-body" }, `${until} 到期`),
    ),
    card(
      "连接信息",
      row("权限", (c.caps || []).join("、")),
      row("范围", c.scope, { copyable: true }),
      row("地域", c.region, { copyable: true }),
      row("外网 Endpoint", c.endpoint, { copyable: true }),
      row("桶域名", c.bucket_url, { copyable: true }),
    ),
    card(
      "在终端里用",
      h(
        "div",
        { class: "cred-tools snippet-tools" },
        eyeButton((shown) => {
          pre.textContent = shown ? env : envMasked;
        }),
        copyButton(() => env, { label: "复制全部" }),
      ),
      pre,
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

// 测试要能单独渲染一份凭证。`main()` 在没有 hash 的时候会立刻走 fail() 分支、
// 不发任何请求，所以导入这个模块不会有副作用。
export { render, MASK };
