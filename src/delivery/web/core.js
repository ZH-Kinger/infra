// 前端公共工具：DOM、请求、平台标签、复制。零依赖、零构建。
//
// 安全约定：接口返回的字段只经 textContent / 属性赋值进入 DOM，绝不拼 innerHTML。

export const PLATFORM_CLASS = { aliyun: "aliyun", volcano: "volcano" };
export const PLATFORM_NAME = { aliyun: "阿里云", volcano: "火山引擎" };

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "hidden") el.hidden = Boolean(value);
    else if (key === "value" && "value" in el) el.value = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// 替换元素的全部子节点：和 h() 一样展开数组、跳过 null / false（原生 replaceChildren 会把 null 写成文字 "null"）
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function mount(...nodes) {
  // 换页时关掉还开着的抽屉（比如表单打开时按了浏览器后退）
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
  const app = document.getElementById("app");
  clear(app);
  app.append(...nodes.flat().filter(Boolean));
  window.scrollTo({ top: 0 });
}

// 只接受同源相对路径，防止接口被篡改后塞进 javascript: 之类的链接。
export function safePath(value, fallback) {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\")
    ? value
    : fallback;
}

// 外部链接只放行 https：云控制台登录地址这类。
export function safeHttps(value) {
  try {
    const u = new URL(value);
    return u.protocol === "https:" ? u.href : "";
  } catch {
    return "";
  }
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function send(path, init) {
  let resp;
  try {
    resp = await fetch(path, { credentials: "same-origin", cache: "no-store", ...init });
  } catch {
    throw new ApiError(0, "连不上服务，请检查网络或稍后再试。");
  }
  let body = {};
  try {
    body = await resp.json();
  } catch {
    body = {};
  }
  if (!resp.ok) {
    const fallback = { 401: "登录已失效。", 403: "没有权限。", 404: "没有找到。" };
    throw new ApiError(resp.status, body.error || fallback[resp.status] || `请求失败（HTTP ${resp.status}）`);
  }
  return body;
}

export function api(path) {
  return send(path, { headers: { Accept: "application/json" } });
}

// 写接口：带 X-Panel-Request 头和 JSON 类型，服务端据此挡跨站请求
export function apiPost(path, body) {
  return send(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-Panel-Request": "1" },
    body: JSON.stringify(body || {}),
  });
}

export function platformTag(platform, display, extraClass) {
  const cls = ["plat", PLATFORM_CLASS[platform] || "", extraClass || ""].join(" ").trim();
  return h("span", { class: cls }, display || PLATFORM_NAME[platform] || platform || "未知平台");
}

export function fmtTime(iso) {
  const t = new Date(iso);
  if (!iso || Number.isNaN(t.getTime())) return "";
  return t.toLocaleString("zh-CN", { hour12: false, month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function ago(iso) {
  const t = new Date(iso);
  if (!iso || Number.isNaN(t.getTime())) return "";
  const min = Math.max(0, (Date.now() - t.getTime()) / 6e4);
  if (min < 1) return "刚刚";
  if (min < 60) return `${Math.floor(min)} 分钟前`;
  if (min < 48 * 60) return `${Math.floor(min / 60)} 小时前`;
  return `${Math.floor(min / 1440)} 天前`;
}

// 复制按钮：复制后按钮文字短暂变成「已复制」
export function copyButton(getText, label = "复制") {
  const btn = h("button", { type: "button", class: "btn tiny ghost" }, label);
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(getText());
      btn.textContent = "已复制";
    } catch {
      btn.textContent = "复制失败，请手动选择";
    }
    setTimeout(() => {
      btn.textContent = label;
    }, 1600);
  });
  return btn;
}

// 右侧抽屉：build(close) 返回抽屉内容。关闭（✕、取消、Esc、点遮罩）后从 DOM 移除，
// 并执行 onClose（比如把地址栏的 #apply=… 还原）。
export function openDrawer(labelledBy, build, onClose) {
  for (const d of document.querySelectorAll("dialog.drawer")) d.remove();
  const dialog = h("dialog", { class: "drawer", "aria-labelledby": labelledBy });
  // 关掉后把焦点还给打开它的按钮，键盘用户不用从页首重新找
  const opener = document.activeElement;
  const close = () => dialog.close();
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (onClose) onClose();
    if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus();
  });
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) close();
  });
  dialog.append(build(close));
  document.body.append(dialog);
  dialog.showModal();
  return close;
}

// 申请单的显示标题：按权限列表申请的单子用策略名（「AliyunOSSReadOnlyAccess 等 3 项」），其余用模板标题
export function requestTitle(r) {
  const policies = (r.template && r.template.policies) || [];
  if (r.template && r.template.id === "policy" && policies.length) {
    return policies.length > 1 ? `${policies[0].name} 等 ${policies.length} 项` : policies[0].name;
  }
  return (r.template && r.template.title) || r.kind_label || r.id;
}
