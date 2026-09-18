// 够 core.js 的 h() / fill() 跑起来的最小 DOM。
//
// 为什么不用 jsdom：面板的前端是零依赖的原生 ES module，为了测它引一个几十 MB 的
// 依赖不划算，而这里要断言的东西（某个轴渲染成了什么控件、值进了哪个键）只用到
// createElement / append / value / addEventListener 这几样。
//
// 这层为什么必须有测试：申请表单是「后端加了一种轴、前端不认识」这类 bug 的唯一藏身处。
// 文本轴那次就是——服务端 1500 条用例全绿，页面上却把它渲染成一个空下拉，配了文本轴的
// 模板在网页上一张都提交不出去。
class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.attrs = {}; this.dataset = {};
    this.className = ""; this.hidden = false; this._value = "";
    this._listeners = {}; this._text = "";
    // classList 和 className 必须是**同一份状态**。真实浏览器里它们就是一个东西 ——
    // 分成两份的话，用 className 断言「加没加 invalid」的用例永远绿，
    // 用 classList 断言「有没有 input 类」的用例永远红，两头都测了个假的
    const classes = () => this.className.split(/\s+/).filter(Boolean);
    const write = (list) => { this.className = [...new Set(list)].join(" "); };
    this.classList = {
      add: (c) => write([...classes(), c]),
      remove: (c) => write(classes().filter((x) => x !== c)),
      contains: (c) => classes().includes(c),
      toggle: (c, on) => (on ?? !classes().includes(c)) ? write([...classes(), c]) : write(classes().filter((x) => x !== c)),
    };
  }
  get value() { return this._value; }
  set value(v) {
    const next = String(v);
    // 真实浏览器里给 <select> 赋一个不存在的值，结果是空串而不是那个值。
    // 不照做的话，选项 id 改了名测试照样绿，而页面上是「什么都没选中」
    if (this.tagName === "SELECT") {
      const opts = this.children.filter((c) => c.tagName === "OPTION").map((c) => c.value);
      this._value = opts.includes(next) ? next : "";
      return;
    }
    this._value = next;
  }
  // textContent 赋值会清掉所有子节点（真实 DOM 就是这样）。不清的话 walk() 会同时
  // 看到新文本和旧的子节点 —— 按文本找元素的用例会找到两份
  get textContent() {
    return this._text || this.children.map((c) => c.textContent).join("");
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...n) {
    for (const x of n) {
      x.parent = this; this.children.push(x);
      // 真实浏览器里 <select> 默认选中第一个 <option>。不照做的话 select.value 恒为空，
      // 「隐藏轴」「预览文案」这类依赖当前选择的逻辑在测试里全都测不到真实行为
      // 判据是「这是第一个 option」，不是「当前值为空」：占位项（<option value="">请选择</option>）
      // 的 value 本来就是空串，按后者会把**第二个** option 当成默认选中，
      // 于是「没选成本归属就该拦住提交」那条校验在测试里永远过、页面上却拦得住
      if (this.tagName === "SELECT" && x.tagName === "OPTION" && !this._sawOption) { this._sawOption = true; this._value = x.value; }
    }
  }
  appendChild(n) { this.append(n); return n; }
  replaceChildren(...n) { this.children = []; this._sawOption = false; this._value = ""; this._text = ""; this.append(...n); }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  removeEventListener() {}
  // **不支持冒泡**：只跑本节点的监听器。所以用事件委托（在父节点上监听）写的交互
  // 在这里完全不触发，而那类断言往往是「没变化就说明对的」形态 —— 会静默通过。
  // 写新交互时把监听挂在元素自己身上，别用委托
  dispatch(t) {
    const e = { target: this, type: t, preventDefault() {}, stopPropagation() {} };
    for (const fn of this._listeners[t] || []) fn(e);
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  remove() {}
  focus() {}
  // 深度优先找出所有后代（测试用）
  *walk() { for (const c of this.children) { yield c; if (c.walk) yield* c.walk(); } }
}
class Text extends Node { constructor(t) { super("#text"); this.textContent = String(t); } }
globalThis.Node = Node;
globalThis.document = {
  createElement: (t) => new Node(t),
  createTextNode: (t) => new Text(t),
  createDocumentFragment: () => new Node("fragment"),
  querySelector: () => null,
  querySelectorAll: () => [],
  body: new Node("body"),
  getElementById: () => null,
};
globalThis.window = { location: { hash: "", pathname: "/" }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };
globalThis.fetch = async () => { throw new Error("测试里不该发网络请求"); };
globalThis.queueMicrotask = globalThis.queueMicrotask || ((fn) => Promise.resolve().then(fn));
export { Node };
