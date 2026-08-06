"use client";

import { useEffect, useMemo, useState } from "react";

type User = { name: string; email: string } | null;
type NavKey = "overview" | "datasets" | "adopt" | "lifecycle" | "runtime" | "access";
type Operation = {
  id: string;
  kind: string;
  dataset: string;
  actor: string;
  status: string;
  workflow: string;
  runUrl: string;
  createdAt: string;
};

const navGroups: Array<{ label: string; items: Array<{ key: NavKey; label: string; description: string; mark: string }> }> = [
  { label: "工作台", items: [
    { key: "overview", label: "运营总览", description: "资源与运行状态", mark: "总" },
  ] },
  { label: "数据管理", items: [
    { key: "datasets", label: "数据资产", description: "数据集与版本", mark: "数" },
    { key: "adopt", label: "存量纳管", description: "OSS / CPFS 接入", mark: "纳" },
    { key: "lifecycle", label: "容量与生命周期", description: "预热、沉淀与回收", mark: "容" },
  ] },
  { label: "计算与安全", items: [
    { key: "runtime", label: "DSW / DLC", description: "开发与训练实例", mark: "算" },
    { key: "access", label: "权限与审计", description: "RAM、挂载与操作记录", mark: "审" },
  ] },
];

const nav = navGroups.flatMap((group) => group.items);

const datasets = [
  { name: "robotics", commit: "9b5d3e6c12", size: "86.4 TB", versions: 18, state: "HEALTHY", hot: 72 },
  { name: "vision-pretrain", commit: "2f84a9d771", size: "41.8 TB", versions: 9, state: "SYNCING", hot: 44 },
  { name: "embodied-eval", commit: "a18c7f640e", size: "8.2 TB", versions: 6, state: "HEALTHY", hot: 93 },
];

const initialOperations: Operation[] = [
  { id: "op-1042", kind: "release", dataset: "robotics", actor: "data.steward", status: "READY", workflow: "dataset-release.yml", runUrl: "", createdAt: "19:22" },
  { id: "op-1041", kind: "lifecycle", dataset: "", actor: "scheduler", status: "PLANNED", workflow: "dataset-lifecycle.yml", runUrl: "", createdAt: "18:17" },
  { id: "op-1039", kind: "runtime", dataset: "vision-pretrain", actor: "ml.engineer", status: "RUNNING", workflow: "pai-runtime.yml", runUrl: "", createdAt: "16:48" },
];

async function submitOperation(body: Record<string, unknown>) {
  const response = await fetch("/api/operations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = (await response.json()) as { error?: string; status?: string; runUrl?: string };
  if (!response.ok) throw new Error(result.error || "操作失败");
  return result;
}

export function OpsConsole({ user }: { user: User }) {
  const [active, setActive] = useState<NavKey>("overview");
  const [operations, setOperations] = useState(initialOperations);
  const [notice, setNotice] = useState("资产指标为界面示例；操作记录来自 D1。所有写操作默认生成计划，执行需要独立审批。 ");
  const [busy, setBusy] = useState(false);
  const [sourceType, setSourceType] = useState<"oss" | "cpfs">("oss");
  const [execute, setExecute] = useState(false);

  useEffect(() => {
    fetch("/api/operations")
      .then((response) => response.json())
      .then((data: { operations?: Operation[] }) => {
        if (data.operations?.length) setOperations(data.operations);
      })
      .catch(() => undefined);
  }, []);

  const title = useMemo(() => nav.find((item) => item.key === active)?.label ?? "运营总览", [active]);

  async function run(body: Record<string, unknown>) {
    setBusy(true);
    try {
      const result = await submitOperation({ ...body, execute });
      setNotice(result.status === "DISPATCHED" ? "已进入受控流水线，等待 Environment 审批。" : "计划已保存，没有修改任何云资源。 ");
      const response = await fetch("/api/operations");
      const data = (await response.json()) as { operations?: Operation[] };
      if (data.operations?.length) setOperations(data.operations);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="console-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-signal">数</span>
          <div><strong>训练数据平台</strong><small>运维控制台</small></div>
        </div>
        <div className="header-account">
          <div className="environment-card">
            <span className="status-dot" />
            <div><small>当前环境</small><strong>杭州生产环境 · 演示数据</strong></div>
          </div>
          <div className="identity">
            <div className="avatar">{(user?.name || "OP").slice(0, 2).toUpperCase()}</div>
            <div><strong>{user?.name || "本地预览"}</strong><small>{user?.email || "只读计划模式"}</small></div>
          </div>
        </div>
      </header>

      <nav className="page-navigation" aria-label="页面导航">
        {nav.map((item) => (
          <button key={item.key} aria-current={active === item.key ? "page" : undefined} className={active === item.key ? "active" : ""} onClick={() => setActive(item.key)}>
            <span className="nav-icon">{item.mark}</span>
            <span className="nav-copy"><strong>{item.label}</strong><small>{item.description}</small></span>
          </button>
        ))}
      </nav>

      <section className="workspace">
        <header className="topbar">
          <div><span className="eyebrow">训练数据平台 / {title}</span><h1>{title}</h1></div>
          <div className="top-actions"><span className="health"><i />服务正常</span><button onClick={() => setActive("adopt")}>纳管数据集</button></div>
        </header>

        <div className="notice"><span>执行策略</span>{notice}</div>

        {active === "overview" && <Overview operations={operations} onNavigate={setActive} />}
        {active === "datasets" && <DatasetInventory />}
        {active === "adopt" && (
          <AdoptPanel sourceType={sourceType} setSourceType={setSourceType} execute={execute} setExecute={setExecute} busy={busy} run={run} />
        )}
        {active === "lifecycle" && <LifecyclePanel execute={execute} setExecute={setExecute} busy={busy} run={run} />}
        {active === "runtime" && <RuntimePanel execute={execute} setExecute={setExecute} busy={busy} run={run} />}
        {active === "access" && <AccessPanel operations={operations} run={run} busy={busy} />}
      </section>
    </main>
  );
}

function Overview({ operations, onNavigate }: { operations: Operation[]; onNavigate: (key: NavKey) => void }) {
  return <div className="page-grid">
    <section className="hero-panel">
      <div><span className="section-label">平台状态</span><h2>训练数据基础设施运行正常</h2><p>数据发布、CPFS 沉降、PAI 注册和训练消费均按不可变版本协议运行。</p></div>
      <div className="fabric-map"><div className="map-node">OSS<small>归档存储</small></div><b>→</b><div className="map-node accent">lakeFS<small>版本管理</small></div><b>→</b><div className="map-node">CPFS<small>训练缓存</small></div><b>→</b><div className="map-node">PAI<small>计算平台</small></div></div>
    </section>
    <div className="metric-row">
      <Metric label="已纳管数据" value="136.4" unit="TB" note="3 个正式数据集" />
      <Metric label="CPFS 使用率" value="68.2" unit="%" note="2.47 PB / 3.60 PB" tone="amber" />
      <Metric label="运行中的实例" value="27" unit="" note="DSW 11 · DLC 16" />
      <Metric label="策略违规" value="0" unit="" note="最近 24 小时" tone="green" />
    </div>
    <section className="panel span-8"><PanelHead title="数据资产" action="查看全部" onClick={() => onNavigate("datasets")} /><DatasetTable compact /></section>
    <section className="panel span-4"><PanelHead title="快捷操作" /><div className="quick-grid">
      <Quick n="01" title="纳管存量数据" text="OSS / CPFS / PAI" onClick={() => onNavigate("adopt")} />
      <Quick n="02" title="容量回收计划" text="只读扫描 · Evict" onClick={() => onNavigate("lifecycle")} />
      <Quick n="03" title="启动训练环境" text="受控 Profile" onClick={() => onNavigate("runtime")} />
      <Quick n="04" title="权限与挂载审计" text="RAM · PAI · POSIX" onClick={() => onNavigate("access")} />
    </div></section>
    <section className="panel span-12"><PanelHead title="最近操作" /><OperationsTable operations={operations} /></section>
  </div>;
}

function Metric({ label, value, unit, note, tone = "blue" }: { label: string; value: string; unit: string; note: string; tone?: string }) {
  return <article className={`metric ${tone}`}><span>{label}</span><div><strong>{value}</strong><em>{unit}</em></div><small>{note}</small></article>;
}

function PanelHead({ title, action, onClick }: { title: string; action?: string; onClick?: () => void }) {
  return <div className="panel-head"><h3>{title}</h3>{action && <button onClick={onClick}>{action} →</button>}</div>;
}

function Quick({ n, title, text, onClick }: { n: string; title: string; text: string; onClick: () => void }) {
  return <button className="quick" onClick={onClick}><span>{n}</span><strong>{title}</strong><small>{text}</small></button>;
}

function DatasetTable({ compact = false }: { compact?: boolean }) {
  return <div className="data-table"><div className="table-row head"><span>数据集</span><span>Commit</span><span>容量</span><span>版本数</span><span>状态</span>{!compact && <span>CPFS 热数据</span>}</div>{datasets.map((item) => <div className="table-row" key={item.name}><span><b className="dataset-mark" /> <strong>{item.name}</strong></span><span className="mono">{item.commit}</span><span>{item.size}</span><span>{item.versions}</span><span><i className={`tag ${item.state.toLowerCase()}`}>{item.state}</i></span>{!compact && <span><b className="bar"><i style={{ width: `${item.hot}%` }} /></b>{item.hot}%</span>}</div>)}</div>;
}

function DatasetInventory() {
  return <div className="page-grid"><section className="panel span-12"><div className="inventory-title"><div><span className="section-label">数据目录</span><h2>受治理的数据资产</h2></div><div className="filter-chip">3 个数据集 · 33 个版本</div></div><DatasetTable /></section><section className="panel span-7"><PanelHead title="版本完整性" /><div className="integrity-list"><p><span>Commit 锁定率</span><strong>100%</strong></p><p><span>Manifest SHA-256 覆盖</span><strong>100%</strong></p><p><span>PAI Version 注册一致性</span><strong>97.4%</strong></p></div></section><section className="panel span-5"><PanelHead title="存储分布" /><div className="donut"><div><strong>136</strong><small>TB 总量</small></div><p><span><i className="blue-dot" /> CPFS 热数据 68%</span><span><i className="dim-dot" /> 仅 OSS 32%</span></p></div></section></div>;
}

function Toggle({ execute, setExecute }: { execute: boolean; setExecute: (value: boolean) => void }) {
  return <label className="execute-toggle"><input type="checkbox" checked={execute} onChange={(e) => setExecute(e.target.checked)} /><span /><div><strong>{execute ? "提交执行" : "仅生成计划"}</strong><small>{execute ? "将进入审批环境" : "不会修改云资源"}</small></div></label>;
}

function AdoptPanel({ sourceType, setSourceType, execute, setExecute, busy, run }: { sourceType: "oss" | "cpfs"; setSourceType: (v: "oss" | "cpfs") => void; execute: boolean; setExecute: (v: boolean) => void; busy: boolean; run: (b: Record<string, unknown>) => void }) {
  const [dataset, setDataset] = useState("robotics-legacy"); const [source, setSource] = useState("oss://legacy-data/robotics"); const [repository, setRepository] = useState("robotics-data"); const [ref, setRef] = useState("robotics-legacy-v1"); const [archive, setArchive] = useState("datasets/robotics-legacy/adopt-v1");
  return <div className="page-grid"><section className="panel span-8 form-panel"><span className="section-label">存量数据纳管</span><h2>将已有数据纳入版本体系</h2><p>平台自动选择安全路径，生成精确 Workflow 请求；原始 CPFS 目录不会被移动。</p><div className="source-tabs"><button className={sourceType === "oss" ? "active" : ""} onClick={() => { setSourceType("oss"); setSource("oss://legacy-data/robotics"); }}>OSS 存量前缀</button><button className={sourceType === "cpfs" ? "active" : ""} onClick={() => { setSourceType("cpfs"); setSource("/mnt/cpfs/users/team/legacy"); }}>CPFS / 已有 PAI</button></div><div className="form-grid"><Field label="数据集短名称" value={dataset} onChange={setDataset} /><Field label="lakeFS Repository" value={repository} onChange={setRepository} /><Field wide label={sourceType === "oss" ? "OSS URI" : "CPFS 挂载路径"} value={source} onChange={setSource} /><Field label="发布 Tag" value={ref} onChange={setRef} /><Field label="归档前缀" value={archive} onChange={setArchive} disabled={sourceType === "oss"} /></div><div className="form-footer"><Toggle execute={execute} setExecute={setExecute} /><button className="primary" disabled={busy} onClick={() => run({ kind: "adopt", sourceType, dataset, source, repository, ref, archivePrefix: archive })}>{busy ? "生成中…" : execute ? "提交纳管并审批" : "生成纳管计划"}</button></div></section><section className="panel span-4"><PanelHead title="自动门禁" /><ol className="gate-list"><li><span>01</span><div><strong>来源登记</strong><small>OSS 前缀必须已进入 Terraform 注册表</small></div></li><li><span>02</span><div><strong>不可变版本</strong><small>Tag 解析为唯一 lakeFS Commit</small></div></li><li><span>03</span><div><strong>内容完整性</strong><small>Manifest 全量 SHA-256 校验</small></div></li><li><span>04</span><div><strong>PAI 注册</strong><small>审批后幂等创建 Dataset Version</small></div></li></ol></section></div>;
}

function Field({ label, value, onChange, wide = false, disabled = false }: { label: string; value: string; onChange: (v: string) => void; wide?: boolean; disabled?: boolean }) { return <label className={wide ? "wide" : ""}><span>{label}</span><input value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} /></label>; }

function LifecyclePanel({ execute, setExecute, busy, run }: { execute: boolean; setExecute: (v: boolean) => void; busy: boolean; run: (b: Record<string, unknown>) => void }) { return <div className="page-grid"><section className="capacity-hero span-12"><div><span className="section-label">CPFS 容量</span><h2>2.47 <em>PB 已使用</em></h2><p>总容量 3.60 PB · 可回收 412 TB · 预计安全水位 56.7%</p></div><div className="capacity-ring"><strong>68%</strong><small>使用率</small></div></section><section className="panel span-7"><PanelHead title="回收候选" /><div className="candidate"><span>robotics / 19b02e…</span><b>138 TB</b><i>34 DAYS</i></div><div className="candidate"><span>vision-pretrain / 84c8d1…</span><b>96 TB</b><i>28 DAYS</i></div><div className="candidate"><span>robotics / a72fd9…</span><b>74 TB</b><i>21 DAYS</i></div><div className="lifecycle-action"><Toggle execute={execute} setExecute={setExecute} /><button className="primary" disabled={busy} onClick={() => run({ kind: "lifecycle" })}>{execute ? "审批并执行 Evict" : "重新生成计划"}</button></div></section><section className="panel span-5"><PanelHead title="安全条件" /><div className="check-list"><p><i>✓</i> 保留每个数据集最近 2 个版本</p><p><i>✓</i> 保护 14 天内发布版本</p><p><i>✓</i> 活动 DSW / DLC 引用自动保留</p><p><i>✓</i> lakeFS Commit 可恢复性确认</p><p><i>✓</i> 只允许 DataFlow Evict，不硬删除</p></div></section></div>; }

function RuntimePanel({ execute, setExecute, busy, run }: { execute: boolean; setExecute: (v: boolean) => void; busy: boolean; run: (b: Record<string, unknown>) => void }) { const [runtime, setRuntime] = useState<"dsw" | "dlc">("dsw"); const [dataset, setDataset] = useState("robotics"); const [commit, setCommit] = useState("9b5d3e6c12a4f8b7c1d0"); return <div className="page-grid"><section className="panel span-8 form-panel"><span className="section-label">PAI 运行环境</span><h2>启动受控训练环境</h2><p>用户只选择版本和 Profile；网络、身份、镜像来源与挂载权限由平台补齐。</p><div className="source-tabs"><button className={runtime === "dsw" ? "active" : ""} onClick={() => setRuntime("dsw")}>DSW 交互开发</button><button className={runtime === "dlc" ? "active" : ""} onClick={() => setRuntime("dlc")}>DLC 分布式训练</button></div><div className="form-grid"><Field label="数据集" value={dataset} onChange={setDataset} /><Field label="Commit" value={commit} onChange={setCommit} /><Field label="镜像 Profile" value="pytorch-2.6" onChange={() => undefined} /><Field label="算力 Profile" value={runtime === "dsw" ? "gpu-dev" : "gpu-training"} onChange={() => undefined} /></div><div className="form-footer"><Toggle execute={execute} setExecute={setExecute} /><button className="primary" disabled={busy} onClick={() => run({ kind: "runtime", runtime, dataset, commit, imageProfile: "pytorch-2.6", computeProfile: runtime === "dsw" ? "gpu-dev" : "gpu-training" })}>{execute ? "提交并进入审批" : "预览完整请求"}</button></div></section><section className="panel span-4"><PanelHead title="固定挂载合同" /><div className="mounts"><p><span>RO</span><strong>/mnt/dataset</strong><small>不可变训练输入</small></p><p><span className="rw">RW</span><strong>/mnt/workspace</strong><small>DSW 个人工作区</small></p><p><span className="rw">RW</span><strong>/mnt/output</strong><small>DLC 输出与 Checkpoint</small></p></div></section><section className="panel span-12"><PanelHead title="活动运行时" /><div className="runtime-row"><span className="pulse" /><strong>dlc-robotics-1042</strong><span>robotics · 9b5d3e6c12</span><i>RUNNING 02:18:42</i><b>8 × ecs.gn7i</b></div><div className="runtime-row"><span className="pulse amber" /><strong>dsw-vision-alice</strong><span>vision-pretrain · 2f84a9d771</span><i>EXPIRES 04:12</i><b>1 × ecs.gn7i</b></div></section></div>; }

function AccessPanel({ operations, run, busy }: { operations: Operation[]; run: (b: Record<string, unknown>) => void; busy: boolean }) { return <div className="page-grid"><section className="panel span-7"><PanelHead title="权限边界" /><div className="role-matrix"><p><strong>Materializer</strong><span>OSS READ · CPFS WRITE</span><i>NO PAI REGISTER</i></p><p><strong>Register</strong><span>PAI VERSION WRITE</span><i>NO DATA READ</i></p><p><strong>Lifecycle</strong><span>CPFS EVICT</span><i>NO DELETE · NO OSS</i></p><p><strong>Runtime</strong><span>DATASET RO · OUTPUT RW</span><i>NO RAW OSS</i></p></div></section><section className="panel span-5"><PanelHead title="合规状态" /><div className="score"><strong>98</strong><span>/ 100</span><p>0 个高风险项<br />2 个配置建议</p></div><button className="wide-button" disabled={busy} onClick={() => run({ kind: "audit", execute: true })}>运行只读挂载审计</button></section><section className="panel span-12"><PanelHead title="操作审计" /><OperationsTable operations={operations} /></section></div>; }

function OperationsTable({ operations }: { operations: Operation[] }) { return <div className="data-table operations"><div className="table-row head"><span>时间</span><span>类型</span><span>数据集</span><span>操作人</span><span>工作流</span><span>状态</span></div>{operations.slice(0, 7).map((op) => <div className="table-row" key={op.id}><span className="mono">{op.createdAt?.slice(11, 16) || op.createdAt}</span><span>{op.kind.toUpperCase()}</span><span>{op.dataset || "ALL"}</span><span>{op.actor}</span><span className="mono">{op.workflow}</span><span><i className={`tag ${op.status.toLowerCase()}`}>{op.status}</i></span></div>)}</div>; }
