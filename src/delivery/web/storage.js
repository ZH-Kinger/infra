// 存储类申请的表单：新建数据目录、数据迁移。
//
// 这两种单子和权限/凭证/开账号的区别在于**申请人填的是路径**，而路径会原样进
// OSS key 和 RAM 策略的 oss:Prefix 条件。所以这里的校验不是体验问题：
// 一个 `*` 或 `../` 就能让一条策略覆盖到别人的数据。凡是能拼出来的就不让人自由填。
//
// 与 requests.js 的分工：那边管抽屉、提交、审批；这边只产出 { fields, read, checks, describe }。

import { h } from "./core.js";

// ── 数据分档与 stage ────────────────────────────────────────────────────────
//
// 这张表是《OSS 存储规范》第一节和第二节的前端副本，**只管怎么说给人听**：
// 哪个桶允许哪些 stage 由模板（后端）决定，这里不做准入判断。
// 之所以放前端，是因为「选了这一类会落到哪、留多久、谁能写」必须在提交前就看得见 ——
// 审批通过之后才发现存错地方，数据已经在那儿了。

const TIERS = {
  A: { label: "拿不回来", hint: "重新采集要花时间，标注要重新花钱" },
  B: { label: "重建很贵", hint: "能重算，代价是机时" },
  C: { label: "能重新拿", hint: "丢了能重下" },
  D: { label: "发布物", hint: "不可变，长期保留" },
};

export const STAGE_META = {
  "raw-ego": { label: "ego 自采原始数据", source: "ego", tier: "A", keep: "数据不自动删，旧版本留 90 天", writer: "采集流程" },
  "raw-robot": { label: "真机采集数据", source: "robot", tier: "A", keep: "数据不自动删，旧版本留 90 天", writer: "采集流程" },
  supplier: { label: "供应商交付", source: "supplier", tier: "A", keep: "数据不自动删，旧版本留 90 天", writer: "外采流程" },
  label: { label: "标注数据", source: "label", tier: "A", keep: "数据不自动删，旧版本留 90 天", writer: "标注人员" },
  rollout: { label: "模型回传数据", source: "rollout", tier: "A", keep: "数据不自动删，旧版本留 90 天", writer: "加工流程" },
  eval: { label: "评测集与评测结论", source: "eval", tier: "A", keep: "数据不自动删，旧版本留 90 天", writer: "加工流程" },
  derived: { label: "加工产出（打标 / 重定向 / 格式转换）", source: "derived", tier: "B", keep: "数据不自动删，旧版本留 30 天", writer: "加工流程" },
  opensource: { label: "开源数据集", source: "public", tier: "C", keep: "数据不自动删，旧版本留 7 天", writer: "导入流程" },
  web: { label: "互联网抓取", source: "web", tier: "C", keep: "数据不自动删，旧版本留 7 天", writer: "导入流程" },
  release: { label: "release 数据", source: "release", tier: "D", keep: "不可变，永久保留", writer: "发布流程" },
};

//: 这些 stage 的数据是花钱买来的或者人工做出来的，manifest 里缺了来源信息就没法自证
const NEEDS_LICENSE = new Set(["opensource", "web", "public-datasets", "internet"]);

export function tierPill(tier) {
  const t = TIERS[tier];
  if (!t) return null;
  return h("span", { class: `pill tier-${tier.toLowerCase()}`, title: t.hint }, `${tier} ${t.label}`);
}

// ── 批次 ID ────────────────────────────────────────────────────────────────
//
// 规范：<采集日期>-<来源>-<场景>[-<序号>]，只用 [A-Za-z0-9][A-Za-z0-9._-]{0,62}。
// **不给自由输入框**：日期用 date 控件、来源由 stage 决定、只有「场景」是人填的，
// 而那一段单独校验。这样拼出来的 ID 不可能含 `/`、`*`、`..` 或空格。

const SCENE = /^[a-z0-9][a-z0-9-]{0,31}$/;
//: 词表类型中间那几层（供应商、来源、版本……）每一段的规则，和后端 flows._BATCH 一致
const SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
//: 词表里「批次」那一层的名字（datatypes.BATCH）。它有专门的输入：日期 + 场景 + 序号
const BATCH_LAYER = "批次ID";

export function batchId(date, source, scene, seq) {
  const day = (date || "").replaceAll("-", "");
  const parts = [day, source, scene].filter(Boolean);
  if (seq) parts.push(String(seq).padStart(2, "0"));
  return parts.join("-");
}

// ── 新建数据目录 ───────────────────────────────────────────────────────────

export function directoryFields(o, { field, update }) {
  const fields = [];
  const read = {};
  const checks = [];

  const buckets = o.buckets || [];
  const bucketOf = (name) => buckets.find((b) => b.name === name) || {};
  //: 词表里的类型（identity/data-types.json）。有它就按它登记的层级逐层问；
  //: 没有的是老分类，走下面写死的 STAGE_META
  const typeOf = (id) => (o.types || {})[id];
  const labelOf = (id) => (typeOf(id) || {}).label || (STAGE_META[id] || {}).label || id;

  // ① 桶
  const bucket = h("select", { id: "f-bucket", class: "input" },
    buckets.map((b) => h("option", { value: b.name }, `${b.name}（${b.region}）`)));
  fields.push(field("f-bucket", "放在哪个桶", bucket, "按数据类型选桶；不确定就先看下面那一栏的说明"));
  read.bucket = () => bucket.value;

  // ② 数据类型 —— 人选的是「这是什么数据」，不是「哪个目录」。
  //    目录是这个选择的结果，不是另一个要填的东西。
  const stage = h("select", { id: "f-stage", class: "input" });
  const stageHint = h("div", { class: "hint" });
  function fillStages() {
    const allowed = bucketOf(bucket.value).stages || [];
    stage.replaceChildren(
      ...allowed.map((id) => h("option", { value: id }, `${labelOf(id)}（${id}）`)),
    );
    showStage();
  }
  function showStage() {
    const t = typeOf(stage.value);
    const meta = STAGE_META[stage.value];
    stageHint.replaceChildren(
      t ? h("span", {}, `目录层级：${stage.value}/${t.layers.map((x) => `<${x}>`).join("/")}/`)
        : meta ? h("span", {}, `${meta.keep}。删除只有管理员能做。`) : h("span", {}, "选一类数据"),
    );
    fillLayers();
  }

  // ②′ 词表类型的中间几层（批次 ID 以外的）：每层一个输入框。
  //     **每一段单独填、单独校验**，不给一个能写 `/` 的整串 —— `a/../b` 能让路径跳出它那一层
  const layersBox = h("div", { class: "field" });
  let segInputs = [];
  function fillLayers() {
    const t = typeOf(stage.value);
    const middle = t ? t.layers.filter((x) => x !== BATCH_LAYER) : [];
    segInputs = middle.map((name, i) => {
      const el = h("input", { id: `f-seg-${i}`, class: "input", maxlength: "63", autocomplete: "off",
        spellcheck: "false", placeholder: name, "aria-label": name });
      el.addEventListener("input", () => { el.classList.remove("invalid"); update(); });
      return el;
    });
    layersBox.replaceChildren(
      ...(segInputs.length ? [h("label", { for: "f-seg-0" }, middle.join(" / ")),
        h("div", { class: "inline" }, ...segInputs),
        h("div", { class: "hint" }, "字母、数字、点、下划线和横线，字母或数字开头")] : []),
    );
    layersBox.hidden = !segInputs.length;
    if (batchRow) batchRow.hidden = Boolean(t) && t.layers[t.layers.length - 1] !== BATCH_LAYER;
  }
  let batchRow = null;
  bucket.addEventListener("change", () => { fillStages(); update(); });
  stage.addEventListener("change", () => { showStage(); update(); });
  fields.push(h("div", { class: "field" },
    h("label", { for: "f-stage" }, "这是什么数据"),
    stage, stageHint));
  read.stage = () => stage.value;
  fields.push(layersBox);

  // ③ 批次 ID：三段拼，只有「场景」是人填的
  const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  const today = iso(new Date());
  const date = h("input", { id: "f-date", class: "input", type: "date", max: today, value: today });
  const scene = h("input", { id: "f-scene", class: "input", maxlength: "32", autocomplete: "off", spellcheck: "false", placeholder: "kitchen" });
  const seq = h("input", { class: "input narrow", type: "number", inputmode: "numeric", min: "1", max: "99", placeholder: "序号", "aria-label": "序号（可选）" });
  const idPreview = h("code", { class: "path-preview" });
  //: 批次 ID 里的「来源」段：老分类由 stage 决定；词表类型的来源已经是单独一层目录，
  //: 不再往批次 ID 里重复一遍
  const sourceOf = () => (typeOf(stage.value) ? "" : (STAGE_META[stage.value] || {}).source);
  const showId = () => {
    idPreview.textContent = batchId(date.value, sourceOf(), scene.value.trim().toLowerCase(), seq.value) || "（还没填完）";
  };
  for (const el of [date, scene, seq]) {
    el.addEventListener("input", () => { el.classList.remove("invalid"); showId(); update(); });
  }
  batchRow = h("div", { class: "field" },
    h("label", { for: "f-scene" }, "批次"),
    h("div", { class: "inline" }, date, scene, seq),
    h("div", { class: "hint" },
      h("span", {}, "采集日期 + 场景（英文小写，如 kitchen、pickplace）。同一天同场景有多批才填序号。"),
      h("div", { class: "id-line" }, "批次 ID ", idPreview)),
  );
  fields.push(batchRow);
  read.batch = () => batchId(date.value, sourceOf(), scene.value.trim().toLowerCase(), seq.value);
  //: 词表类型按层提交：中间几层 + 最后的批次 ID（如果这个类型有批次这一层）
  read.segments = () => {
    const t = typeOf(stage.value);
    if (!t) return undefined;
    const vals = segInputs.map((el) => el.value.trim());
    return t.layers[t.layers.length - 1] === BATCH_LAYER ? [...vals, read.batch()] : vals;
  };
  checks.push(() => {
    const bad = segInputs.find((el) => !SEGMENT.test(el.value.trim()));
    return bad ? [bad, `「${bad.placeholder}」要填，只能用字母、数字、点、下划线和横线，字母或数字开头。`] : "";
  });
  checks.push(() => (batchRow.hidden || SCENE.test(scene.value.trim().toLowerCase())
    ? ""
    : [scene, scene.value.trim() ? "场景只能用小写字母、数字和横线，字母或数字开头。" : "请填一下场景，比如 kitchen。"]));
  checks.push(() => (batchRow.hidden || (date.value && date.value <= today) ? "" : [date, "采集日期不能晚于今天。"]));

  // ④ 许可证 / 来源 —— 只对开源和互联网数据要，而且是硬要求
  const license = h("input", { id: "f-license", class: "input", maxlength: "80", autocomplete: "off", placeholder: "例如 CC-BY-4.0，或抓取来源站点" });
  const licenseRow = h("div", { class: "field" },
    h("label", { for: "f-license" }, "许可证 / 来源站点"),
    license,
    h("div", { class: "hint" }, "出合规问题时这是唯一能自证的东西。开源数据填许可证，抓取数据填来源站点。"));
  license.addEventListener("input", () => { license.classList.remove("invalid"); update(); });
  fields.push(licenseRow);
  read.license = () => license.value.trim();
  checks.push(() => (!NEEDS_LICENSE.has(stage.value) || license.value.trim()
    ? "" : [license, "开源和互联网数据必须填来源，否则出合规问题没法自证。"]));

  // ⑤ 估算大小 —— 影响的是别人（配额、跨区同步排期），不是自己
  const size = h("input", { id: "f-size", class: "input narrow", type: "number", inputmode: "decimal", min: "0", step: "0.1", placeholder: "0" });
  const sizeUnit = h("select", { class: "input narrow", "aria-label": "容量单位" },
    [h("option", { value: "GiB" }, "GiB"), h("option", { value: "TiB" }, "TiB")]);
  for (const el of [size, sizeUnit]) el.addEventListener("input", update);
  sizeUnit.addEventListener("change", update);
  fields.push(field("f-size", "大概多大（可选）", h("div", { class: "inline" }, size, sizeUnit),
    "超过 10 TiB 的批次审批人会想知道，跨地域同步也要按这个排期"));
  read.size = () => (Number.parseFloat(size.value) > 0 ? `${size.value}${sizeUnit.value}` : "");

  function update_() {
    fillStages();
    showId();
    licenseRow.hidden = !NEEDS_LICENSE.has(stage.value);
  }
  // stage 变了要连带显示/隐藏许可证那一栏
  stage.addEventListener("change", () => { licenseRow.hidden = !NEEDS_LICENSE.has(stage.value); });
  bucket.addEventListener("change", () => { licenseRow.hidden = !NEEDS_LICENSE.has(stage.value); });
  update_();

  const pathOf = (p) => {
    if (!p.bucket || !p.stage) return "";
    const tail = Array.isArray(p.segments) ? p.segments : [p.batch];
    return `${p.bucket}/${p.stage}/${tail.map((x) => x || "…").join("/")}/`;
  };
  function describe(p) {
    if (!p.bucket || !p.stage) return "选完桶和数据类型就能看到完整路径。";
    const meta = STAGE_META[p.stage];
    const tail = meta ? `${meta.keep}。写入身份是${meta.writer}，` : "这个目录由流程写入，";
    return `建目录 ${pathOf(p)}，同时写入 _manifest.json（来源、负责人、QC 状态）。`
      + `${tail}你本人对它只读 —— 要往里放数据走「访问凭证」申请，或者由流程写入。`;
  }

  return { fields, read, checks, describe, pathOf };
}

// ── 新增数据类型 ───────────────────────────────────────────────────────────
//
// 规范要求：所有桶的一级目录只能从一张词表里取，**新增要审批**。
// 审批通过后后端直接写进 identity/data-types.json，「新建数据目录」里就能选到它。

const TYPE_KEY = /^[a-z][a-z0-9-]{1,31}$/;
//: 这些名字在规范里有固定含义（和 datatypes.RESERVED 一致）。拿 tmp 当类型的话，
//: tmp/ 下的数据会被 7 天清理规则一起删掉
const RESERVED = new Set(["general", "tmp", "staging", "qc", "_misc", "_staging"]);

export function splitLayers(text) {
  return String(text || "").split(/[\s,，/、]+/).map((x) => x.trim()).filter(Boolean);
}

export function datatypeFields(o, { field, update }) {
  const fields = [];
  const read = {};
  const checks = [];
  const known = o.types || {};

  const key = h("input", { id: "f-dt-key", class: "input", maxlength: "32", autocomplete: "off", spellcheck: "false", placeholder: "例如 sim-data" });
  fields.push(field("f-dt-key", "英文名（就是一级目录名）", key,
    `小写字母、数字和横线。已有：${Object.keys(known).join("、") || "（还没有）"}`));
  read.key = () => key.value.trim();
  checks.push(() => {
    const v = key.value.trim();
    if (!TYPE_KEY.test(v)) return [key, "英文名只能用小写字母、数字和横线，字母开头，2～32 个字符。"];
    if (RESERVED.has(v)) return [key, `${v} 是规范里有固定含义的目录名，不能当数据类型。`];
    if (known[v]) return [key, `已经有 ${v}（${known[v].label}）了。`];
    return "";
  });

  const label = h("input", { id: "f-dt-label", class: "input", maxlength: "20", autocomplete: "off", placeholder: "例如 仿真数据" });
  fields.push(field("f-dt-label", "中文名", label, "申请页上给人看的名字"));
  read.label = () => label.value.trim();
  checks.push(() => (label.value.trim() ? "" : [label, "中文名要填。"]));

  const layers = h("input", { id: "f-dt-layers", class: "input", autocomplete: "off", value: "来源 批次ID" });
  const preview = h("code", { class: "path-preview" });
  const show = () => { preview.textContent = `${key.value.trim() || "<英文名>"}/${splitLayers(layers.value).map((x) => `<${x}>`).join("/")}/`; };
  fields.push(h("div", { class: "field" },
    h("label", { for: "f-dt-layers" }, "下面几层"),
    layers,
    h("div", { class: "hint" }, "用空格或逗号隔开，最多 4 层；有批次的话「批次ID」放最后。",
      h("div", { class: "id-line" }, "目录长这样 ", preview))));
  read.layers = () => splitLayers(layers.value);
  checks.push(() => {
    const got = splitLayers(layers.value);
    if (!got.length || got.length > 4) return [layers, "层级要有 1～4 层。"];
    if (new Set(got).size !== got.length) return [layers, "层名重复了。"];
    if (got.includes("批次ID") && got[got.length - 1] !== "批次ID") return [layers, "「批次ID」只能是最后一层。"];
    return "";
  });

  const boxes = (o.buckets || []).map((b) => {
    const box = h("input", { type: "checkbox", class: "check", value: b.name, id: `f-dt-b-${b.name}` });
    box.addEventListener("change", update);
    return { box, row: h("label", { class: "check-row", for: `f-dt-b-${b.name}` }, box, `${b.name}（${b.region}）`) };
  });
  fields.push(h("div", { class: "field" }, h("label", {}, "用在哪些桶"), ...boxes.map((x) => x.row),
    h("div", { class: "hint" }, "同一个类型在所有桶里层级都一样，搬运才能只换桶名。")));
  read.buckets = () => boxes.filter((x) => x.box.checked).map((x) => x.box.value);
  checks.push(() => (boxes.some((x) => x.box.checked) ? "" : [boxes[0]?.box, "至少选一个桶。"]));

  for (const el of [key, label, layers]) {
    el.addEventListener("input", () => { el.classList.remove("invalid"); show(); update(); });
  }
  show();

  function describe(p) {
    if (!p.key) return "填完英文名就能看到目录的样子。";
    return `审批通过后 ${p.key}/${(p.layers || []).map((x) => `<${x}>`).join("/")}/ 会加入数据类型词表，`
      + `${(p.buckets || []).join("、") || "（还没选桶）"} 的「新建数据目录」里就能选它。`;
  }
  return { fields, read, checks, describe };
}

// ── 数据迁移 ───────────────────────────────────────────────────────────────
//
// 用户填两个路径，链路由这里判断。**六条搬运链已经都在跑**（bot 侧），
// 这个表只是把「该用哪条」从人的脑子里挪到代码里 ——
// 今天是靠飞书意图关键词抢着匹配的，谁的话术排在前面谁赢。

const URI = /^(oss|tos|cpfs|vepfs):\/\/([A-Za-z0-9][A-Za-z0-9._-]{1,62})(\/.*)?$/;

export function parseUri(text) {
  const raw = (text || "").trim();
  if (!raw) return { empty: true };
  const m = URI.exec(raw);
  if (!m) return { error: "格式是 oss://桶名/目录/ 这样。也支持 tos:// cpfs:// vepfs://" };
  const [, scheme, bucket, rest = "/"] = m;
  const prefix = rest.replace(/^\/+/, "");
  if (prefix.includes("//")) return { error: "路径里有连续的斜杠。" };
  if (/(^|\/)\.\.?(\/|$)/.test(prefix)) return { error: "路径里不能有 . 或 ..。" };
  if (/[*?\\]|\s/.test(prefix)) return { error: "路径里不能有空格、通配符或反斜杠。" };
  if (prefix && !prefix.endsWith("/")) return { error: "只能迁移目录，结尾要有 /。" };
  return { scheme, bucket, prefix, uri: `${scheme}://${bucket}/${prefix}` };
}

//: 地域分组。杭州是入口和主本，其余是副本所在地（见《OSS 存储规范》第二节）

function regionOf(u, buckets) {
  const b = (buckets || []).find((x) => x.name === u.bucket);
  return b ? String(b.region || "").replace(/^oss-/, "") : "";
}

//: 并行文件系统和对象存储之间的数据流动 —— 预热和沉降。**面板接了这四条。**
//:
//: 方向由地址推出来，不让人选：让人选「这是预热还是沉降」的后果不是报错，
//: 是把数据往相反方向覆盖一遍，而那不可逆。
const DATAFLOW = {
  "oss->cpfs": { label: "预热到 CPFS", note: "从 OSS 把数据加载进 CPFS。走阿里数据流动。" },
  "cpfs->oss": { label: "从 CPFS 沉降", note: "把 CPFS 上的改动刷回 OSS。走阿里数据流动。" },
  "tos->vepfs": { label: "预热到 vePFS", note: "从 TOS 把数据加载进 vePFS。走火山数据流动。" },
  "vepfs->tos": { label: "从 vePFS 沉降", note: "把 vePFS 上的改动刷回 TOS。走火山数据流动。" },
};

//: 这几条**确实没接**，而且不是「还没做」是「做不了」：两个并行文件系统之间
//: 没有直连，要三段（沉降 → 跨云 → 预热）。跨云那一段还得分开提。
const NO_CHAIN = {
  "vepfs->cpfs": "vePFS 到 CPFS",
  "cpfs->vepfs": "CPFS 到 vePFS",
  "cpfs->cpfs": "CPFS 之间",
  "vepfs->vepfs": "vePFS 之间",
};

/**
 * 判断两个地址之间走哪条链。纯函数，没有网络请求 —— 提交前就要能看见结论。
 * 返回 { chain, label, hops[], note, warn, error }。
 */
export function routeOf(src, dst, buckets) {
  if (src.empty || dst.empty || src.error || dst.error) return {};
  if (src.uri === dst.uri) return { error: "源和目标是同一个地方。" };
  const pair = `${src.scheme}->${dst.scheme}`;
  const sr = regionOf(src, buckets);
  const dr = regionOf(dst, buckets);

  if (pair === "oss->oss") {
    if (sr && dr && sr !== dr) {
      // **这里以前画的是「源（杭州）→ wuji-sing（新加坡）→ 目的」，还写着
      // 「副本 30 天后自动清理」—— 两句都不是真的：**
      //   · 后端走阿里在线迁移直连两个桶，没有新加坡中转那一跳
      //   · 全仓库没有任何 30 天清理的实现
      // 而且 2026-09-21 实测：曼谷 5 个一级目录杭州一个都没有、中转桶有 9 个孤本
      // —— 那句「30 天自动清理」真去实现就是删掉只此一份的数据。
      // 申请人是照这些话决定要不要提单的，画一条不存在的链比不画更糟。
      return {
        chain: "bucket-transfer",
        label: "跨地域复制",
        hops: [`${src.bucket}（${sr}）`, `${dst.bucket}（${dr}）`],
        note: "同账号跨地域，目的端直接从源端拉，不走中转。",
        warn: "**搬过去的这份不会自动清理**，要不要留、留多久由你自己管。"
          + "目的地域已经有同名对象时按你选的同名策略处理。",
      };
    }
    return { chain: "bucket-transfer", label: "同地域桶间迁移", hops: [src.bucket, dst.bucket], note: "同账号同地域，直接复制。" };
  }
  if (pair === "tos->tos") return { chain: "bucket-transfer", label: "火山桶间迁移", hops: [src.bucket, dst.bucket], note: "火山同账号内复制。" };
  if (pair === "tos->oss") return { chain: "transfer", label: "跨云迁移（火山 → 阿里）", hops: [src.bucket, dst.bucket], note: "走阿里在线迁移服务。" };
  if (pair === "oss->tos") {
    return {
      chain: "transfer", label: "跨云迁移（阿里 → 火山）", hops: [src.bucket, dst.bucket],
      note: "走火山迁移服务。",
      warn: "火山侧只能指定目标桶、指不了目标目录 —— 对象会按源路径结构落进目标桶。",
    };
  }
  if (DATAFLOW[pair]) {
    const fs = pair.startsWith("cpfs") || pair.startsWith("vepfs") ? src : dst;
    return {
      chain: "dataflow",
      label: DATAFLOW[pair].label,
      hops: [src.bucket, dst.bucket],
      note: DATAFLOW[pair].note,
      // **说清楚面板不会替人建绑定。** 阿里那边对已有数据的目录建数据流动会把它清空，
      // 所以面板只复用现成的绑定；没有绑定时提交会被拒，而那句话要提前让人看见
      warn: pair.includes("cpfs")
        ? `目录 ${fs.bucket} 上必须**已经有数据流动绑定** —— 面板不会替你建。`
          + "没有绑定的话审批通过后执行会失败（建绑定会清空目录，那一步要人去控制台确认）。"
        : "vePFS 与 TOS 必须同地域，而且要先开好服务访问授权、数据流动带宽 > 0。",
    };
  }
  if (NO_CHAIN[pair]) {
    return {
      error: `${NO_CHAIN[pair]}搬不了 —— 并行文件系统之间没有直连。`
        + "要先沉降到对象存储、跨过去、再预热回来，这三段现在要分开提。",
    };
  }
  return { error: `${src.scheme} → ${dst.scheme} 没有现成的链路，请找管理员。` };
}

export function transferFields(o, { field, update }) {
  const fields = [];
  const read = {};
  const checks = [];
  const buckets = o.buckets || [];

  const mkPath = (id, label, placeholder, hint) => {
    const input = h("input", { id, class: "input mono", autocomplete: "off", spellcheck: "false", placeholder });
    const err = h("div", { class: "hint path-err", hidden: true });
    input.addEventListener("input", () => { input.classList.remove("invalid"); update(); });
    const row = h("div", { class: "field" }, h("label", { for: id }, label), input, err, h("div", { class: "hint" }, hint));
    return { input, err, row };
  };

  const src = mkPath("f-src", "从哪里", "oss://wuji-bucket-hangzhou/supplier/20260920-ego-kitchen/",
    "目录要以 / 结尾。面板接的是对象存储：oss:// 和 tos://（同云、跨云都行）。");
  const dst = mkPath("f-dst", "到哪里", "oss://wuji-bangkok/supplier/20260920-ego-kitchen/", "目标目录不存在会自动建。");
  fields.push(src.row, dst.row);
  read.source = () => src.input.value.trim();
  read.dest = () => dst.input.value.trim();

  const showErr = (box, parsed) => {
    box.err.textContent = parsed.error || "";
    box.err.hidden = !parsed.error;
  };
  checks.push(() => {
    const p = parseUri(src.input.value);
    return p.uri ? "" : [src.input, p.error || "请填源路径。"];
  });
  checks.push(() => {
    const p = parseUri(dst.input.value);
    return p.uri ? "" : [dst.input, p.error || "请填目标路径。"];
  });
  checks.push(() => {
    const r = routeOf(parseUri(src.input.value), parseUri(dst.input.value), buckets);
    return r.error ? [dst.input, r.error] : "";
  });

  // 同名文件怎么办 —— 默认跳过。覆盖是不可逆的，不该是默认值
  const overwrite = h("select", { id: "f-overwrite", class: "input" },
    [h("option", { value: "skip" }, "跳过（目标已有同名文件就不动它）"),
     h("option", { value: "overwrite" }, "覆盖（用源文件替换）")]);
  overwrite.addEventListener("change", update);
  fields.push(field("f-overwrite", "遇到同名文件", overwrite, "覆盖不可逆。目标桶开了版本控制的话旧版本还在，没开就真没了。"));
  read.overwrite = () => overwrite.value;

  // 路由结论：填完两个路径立刻可见，不等提交
  const routeBox = h("div", { class: "route-box" });
  function renderRoute() {
    const s = parseUri(src.input.value);
    const d = parseUri(dst.input.value);
    showErr(src, s);
    showErr(dst, d);
    const r = routeOf(s, d, buckets);
    if (r.error) {
      routeBox.replaceChildren(h("p", { class: "route-bad" }, r.error));
      return;
    }
    if (!r.chain) {
      routeBox.replaceChildren(h("p", { class: "muted" }, "填完两个路径就会显示走哪条链路。"));
      return;
    }
    routeBox.replaceChildren(
      h("div", { class: "route-head" }, h("strong", {}, r.label)),
      h("div", { class: "route-hops" }, r.hops.flatMap((hop, i) => (i ? [h("span", { class: "route-arrow" }, "→"), h("code", {}, hop)] : [h("code", {}, hop)]))),
      h("p", { class: "muted" }, r.note),
      r.warn ? h("p", { class: "route-warn" }, r.warn) : null,
      h("p", { class: "muted small" }, "搬多少由后台估算后写进审批单，审批人看得到。"),
    );
  }
  src.input.addEventListener("input", renderRoute);
  dst.input.addEventListener("input", renderRoute);
  renderRoute();
  fields.push(h("div", { class: "field" }, h("label", {}, "走哪条链路"), routeBox));

  function describe(p) {
    const r = routeOf(parseUri(p.source), parseUri(p.dest), buckets);
    if (!r.chain) return "填完两个路径就能看到会怎么搬。";
    return `后台按「${r.label}」把 ${p.source} 搬到 ${p.dest}，同名文件${p.overwrite === "overwrite" ? "覆盖" : "跳过"}。`
      + "进度和结果会回到「我的申请」，搬完会做端到端校验 —— 不采信搬运器自己报的成功。";
  }

  return { fields, read, checks, describe };
}
