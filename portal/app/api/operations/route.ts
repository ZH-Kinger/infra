import { desc } from "drizzle-orm";
import { env } from "cloudflare:workers";
import { ensureDb } from "../../../db";
import { operations } from "../../../db/schema";
import { getChatGPTUser } from "../../chatgpt-auth";

type ActionKind = "adopt" | "release" | "runtime" | "lifecycle" | "audit";

type OperationRequest = {
  kind?: ActionKind;
  execute?: boolean;
  dataset?: string;
  sourceType?: "oss" | "cpfs";
  source?: string;
  repository?: string;
  ref?: string;
  archivePrefix?: string;
  runtime?: "dsw" | "dlc";
  commit?: string;
  imageProfile?: string;
  computeProfile?: string;
};

function runtimeEnv(name: string): string {
  const bindings = env as unknown as Record<string, unknown>;
  return typeof bindings[name] === "string" ? String(bindings[name]).trim() : "";
}

function safePart(value: unknown, field: string): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text || text.length > 180 || /[\r\n]/.test(text)) {
    throw new Error(`${field} 不合法`);
  }
  return text;
}

function workflowFor(payload: OperationRequest) {
  const kind = payload.kind;
  if (!kind) throw new Error("缺少操作类型");

  if (kind === "adopt" || kind === "release") {
    const dataset = safePart(payload.dataset, "dataset");
    const repository = safePart(payload.repository, "repository");
    const ref = safePart(payload.ref, "ref");
    const source = safePart(payload.source, "source");
    if (payload.sourceType === "oss") {
      const normalized = source.replace(/^oss:\/\//, "");
      const slash = normalized.indexOf("/");
      if (slash < 1 || slash === normalized.length - 1) {
        throw new Error("OSS 来源必须是 oss://bucket/prefix");
      }
      return {
        workflow: "dataset-release.yml",
        inputs: {
          mode: "oss-ingest",
          dataset,
          repository,
          ref,
          source_bucket: normalized.slice(0, slash),
          source_prefix: normalized.slice(slash + 1),
        },
      };
    }
    if (payload.sourceType === "cpfs") {
      return {
        workflow: "dataset-release.yml",
        inputs: {
          mode: "cpfs-adopt",
          dataset,
          repository,
          ref,
          prepared_dir: source,
          archive_prefix: safePart(payload.archivePrefix, "archivePrefix"),
        },
      };
    }
    throw new Error("sourceType 必须是 oss 或 cpfs");
  }

  if (kind === "runtime") {
    return {
      workflow: "pai-runtime.yml",
      inputs: {
        runtime: payload.runtime === "dlc" ? "dlc" : "dsw",
        dataset: safePart(payload.dataset, "dataset"),
        commit: safePart(payload.commit, "commit"),
        image_profile: safePart(payload.imageProfile, "imageProfile"),
        compute_profile: safePart(payload.computeProfile, "computeProfile"),
        execute: Boolean(payload.execute),
      },
    };
  }

  if (kind === "lifecycle") {
    return {
      workflow: "dataset-lifecycle.yml",
      inputs: { min_age_days: "14", keep_last: "2", execute: Boolean(payload.execute) },
    };
  }

  return { workflow: "pai-mount-audit.yml", inputs: { kind: "both" } };
}

async function saveOperation(values: typeof operations.$inferInsert) {
  const db = await ensureDb();
  await db.insert(operations).values(values);
}

export async function GET() {
  try {
    const db = await ensureDb();
    const rows = await db.select().from(operations).orderBy(desc(operations.createdAt)).limit(30);
    return Response.json({ operations: rows });
  } catch {
    return Response.json({ operations: [] });
  }
}

export async function POST(request: Request) {
  try {
    const payload = (await request.json()) as OperationRequest;
    const execute = Boolean(payload.execute);
    const user = await getChatGPTUser();
    const actor = user?.email ?? "local-preview";
    const operation = workflowFor(payload);
    const id = crypto.randomUUID();
    let status = "PLANNED";
    let runUrl = "";

    if (execute) {
      if (!user) {
        return Response.json({ error: "真实执行需要登录受保护的管理站点" }, { status: 401 });
      }
      const admins = runtimeEnv("OPS_ADMIN_EMAILS")
        .split(",")
        .map((item) => item.trim().toLowerCase())
        .filter(Boolean);
      if (!admins.includes(user.email.toLowerCase())) {
        return Response.json({ error: "当前账号没有执行权限，只能生成计划" }, { status: 403 });
      }

      const token = runtimeEnv("GITHUB_TOKEN");
      const repository = runtimeEnv("GITHUB_REPOSITORY") || "ZH-Kinger/infra";
      const ref = runtimeEnv("GITHUB_REF") || "main";
      if (!token) {
        return Response.json({ error: "尚未配置 GitHub 执行凭证" }, { status: 503 });
      }
      const response = await fetch(
        `https://api.github.com/repos/${repository}/actions/workflows/${operation.workflow}/dispatches`,
        {
          method: "POST",
          headers: {
            Accept: "application/vnd.github+json",
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
            "User-Agent": "dataset-ops-console",
            "X-GitHub-Api-Version": "2022-11-28",
          },
          body: JSON.stringify({ ref, inputs: operation.inputs }),
        },
      );
      if (!response.ok) {
        throw new Error(`GitHub Workflow 触发失败（${response.status}）`);
      }
      status = "DISPATCHED";
      runUrl = `https://github.com/${repository}/actions/workflows/${operation.workflow}`;
    }

    await saveOperation({
      id,
      kind: payload.kind ?? "unknown",
      dataset: payload.dataset?.trim() ?? "",
      mode: execute ? "execute" : "plan",
      actor,
      status,
      workflow: operation.workflow,
      payload: JSON.stringify(operation.inputs),
      runUrl,
    });

    return Response.json({ id, status, runUrl, workflow: operation.workflow, inputs: operation.inputs });
  } catch (error) {
    const message = error instanceof Error ? error.message : "操作失败";
    return Response.json({ error: message }, { status: 400 });
  }
}
