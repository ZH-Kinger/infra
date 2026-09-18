// 「怎么用起来」：每个平台的下一步动作 + 命令行用法。
//
// 平台那部分的文案来自 /api/access，和 `delivery login-guide` 同一份逻辑 ——
// 网页上手写第二份的话，两边迟早对不上，而这正是用户照着做的东西。
//
// 厂商 CLI 那部分是手写的，但每一条都是实测过的，不是抄文档：
// 火山的环境变量优先级最低、tosutil 根本不读环境变量，这两条文档上都查不到。

import { api, h, mount } from "./core.js";

function size(bytes) {
  return bytes >= 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;
}

function downloadRow(f) {
  return h(
    "div",
    { class: "perm" },
    h("code", {}, f.name),
    h("span", { class: "muted" }, `${size(f.size)} · sha256 ${f.sha256.slice(0, 16)}…`),
    h("a", { class: "btn ghost small", href: `/download/${encodeURIComponent(f.name)}` }, "下载"),
  );
}

const STATE = { ready: ["good", "可以直接用"], action: ["warn", "要做一次"], blocked: ["crit", "要找管理员"] };

function code(text) {
  return h("pre", { class: "snippet-body" }, text);
}

function section(title, ...body) {
  return h("section", { class: "group" }, h("div", { class: "group-label" }, title), ...body);
}

function platformCard(p) {
  const [tone, label] = STATE[p.state] || ["", p.state];
  return h(
    "div",
    { class: "card opt" },
    h(
      "div",
      { class: "opt-main" },
      h("div", { class: "opt-title" }, h("span", { class: "opt-name" }, p.display), h("span", { class: `pill ${tone}` }, label)),
      h("p", { class: "opt-desc" }, p.headline),
      h("div", { class: "opt-meta" }, p.hint),
      p.accounts.length ? h("div", { class: "opt-meta" }, `你的账号：${p.accounts.join("、")}`) : null,
      p.next_command ? code(p.next_command) : null,
    ),
    h(
      "div",
      { class: "opt-side" },
      p.console_url ? h("a", { class: "btn ghost small", href: p.console_url, target: "_blank", rel: "noreferrer noopener" }, "打开控制台") : null,
    ),
  );
}

// 每条都是实测结论。凭证怎么喂给厂商 CLI 是使用方最容易卡住的地方，
// 而两家的坑方向相反：阿里能纯环境变量，火山的环境变量优先级最低。
const VENDORS = [
  {
    name: "阿里云 aliyun",
    install: "单文件二进制，无依赖。钉版本从 GitHub Release 下 —— CDN 的 latest 直链没有校验和文件。",
    links: [["GitHub Release（带 sha256）", "https://github.com/aliyun/aliyun-cli/releases"]],
    body: [
      ["用申请到的临时凭证（带 SecurityToken）", `export ALIBABA_CLOUD_ACCESS_KEY_ID=STS.xxxx
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=xxxx
export ALIBABA_CLOUD_SECURITY_TOKEN=xxxx
export ALIBABA_CLOUD_REGION_ID=cn-hangzhou
export ALIBABA_CLOUD_IGNORE_PROFILE=TRUE`],
      ["查某个接口要传什么", "aliyun ecs DescribeInstances --cli-section request --help --cli-output json"],
    ],
    notes: [
      "最后那行 IGNORE_PROFILE 别省：你机器上已有的 ~/.aliyun/config.json 会盖掉环境变量。",
      "查 VPC 和交换机要用 vpc 产品（ecs 下的同名接口已废弃），查安全组反而要用 ecs 产品。",
    ],
  },
  {
    name: "火山引擎 ve",
    install: "npm 包 @volcengine/cli，命令叫 ve（不是 volc）。v1.0.20 起从 volcengine-cli 改的前缀。",
    links: [["GitHub Release", "https://github.com/volcengine/volcengine-cli/releases"],
            ["官方文档", "https://www.volcengine.com/docs/6291/65568"]],
    body: [["配凭证", "ve configure --profile wuji"]],
    notes: [
      "凭证优先级是 --profile > 当前 profile > VOLCENGINE_PROFILE > 环境变量 —— 环境变量最低。你机器上只要存在 profile，export 的会被静默忽略、用成你自己的账号。",
      "查安全组在 vpc 产品下，不在 ecs 下（和阿里正好相反）。子网叫 Subnet 不叫 VSwitch。",
      "没有自动翻页，列资源要自己写循环。",
    ],
  },
  {
    name: "火山 TOS tosutil",
    install: "对象存储的数据面工具，和 ve 是两个东西。",
    links: [["官方下载", "https://www.volcengine.com/docs/6349/148776"]],
    body: [["配凭证", "tosutil config -i=<AK> -k=<SK> -e=tos-cn-shanghai.volces.com"]],
    notes: [
      "它完全不读凭证环境变量，只能写配置文件。",
      "写完 chmod 600 ~/.tosutilconfig —— 默认权限是 664，同机其他账号能读到你的 AK。",
    ],
  },
  {
    name: "九章 aladdin",
    install: "九章没有公开下载地址，二进制由本平台托管，见下面的「工具下载」。五个平台都有。",
    body: [["首次绑定", "aladdin init"], ["看能做什么", "aladdin spec -o json"]],
    notes: [
      "AK/SK 要在控制台自助签发，平台限制这一步无法自动化，所以绑一次就好。",
      "九章只有机器学习平台的大卡，开不了通用服务器。",
    ],
  },
];

function vendorCard(v) {
  return h(
    "div",
    { class: "card opt" },
    h(
      "div",
      { class: "opt-main" },
      h("div", { class: "opt-title" }, h("span", { class: "opt-name" }, v.name)),
      h("p", { class: "opt-desc" }, v.install),
      ...v.body.flatMap(([label, text]) => [h("div", { class: "opt-meta" }, label), code(text)]),
      v.links ? h("div", { class: "opt-meta" }, v.links.map(([label, url], i) => h("span", {}, i ? " · " : "", h("a", { href: url, target: "_blank", rel: "noreferrer noopener" }, label)))) : null,
      v.notes.length ? h("ul", { class: "opt-notes" }, v.notes.map((n) => h("li", {}, n))) : null,
    ),
  );
}

export function accessRoutes({ load, errorView }) {
  return {
    access(ctx) {
      return load(
        async () => ({ ...(await api("/api/access")), ...(await api("/api/downloads")) }),
        (data) =>
          mount(
            h("header", { class: "masthead" }, h("h1", {}, "CLI"), h("p", { class: "lede" }, "命令行怎么用，以及你在每个平台的下一步。")),
            section("平台", ...data.platforms.map(platformCard)),
            section(
              "面板命令行",
              h(
                "div",
                { class: "card opt" },
                h(
                  "div",
                  { class: "opt-main" },
                  h("p", { class: "opt-desc" }, "网页能做的事命令行都能做，提的申请两边互通。"),
                  h("div", { class: "opt-meta" }, "登录（飞书浏览器授权，或公司 IAM 设备码）"),
                  code("delivery login\ndelivery login --iam"),
                  h("div", { class: "opt-meta" }, "看能申请什么、提申请"),
                  code("delivery request templates\ndelivery request new <模板> --reason '...'"),
                  h("div", { class: "opt-meta" }, "按权限策略申请"),
                  code("delivery request policies --search oss\ndelivery request grant --account <平台/账号ID> --policy <策略名> --days 30 --reason '...'"),
                  h("div", { class: "opt-meta" }, "policies 不加 --account 就列出你所有云账号的，账号 ID 从那里抄。"),
                  h("div", { class: "opt-meta" }, "查自己的申请"),
                  code("delivery request list\ndelivery request show <申请单号>"),
                  h("ul", { class: "opt-notes" }, [
                    h("li", {}, "访问凭证不在命令行里领。审批通过后直接发到对应飞书审批的评论里，面板和命令行都不保存。"),
                  ]),
                ),
              ),
            ),
            section("厂商命令行", ...VENDORS.map(vendorCard)),
            data.files && data.files.length
              ? section(
                  "工具下载",
                  h(
                    "div",
                    { class: "card" },
                    h("p", { class: "opt-desc pad-block" }, "下完核对 sha256 再用。"),
                    ...data.files.map(downloadRow),
                  ),
                )
              : null,
          ),
        ctx,
      );
    },
  };
}
