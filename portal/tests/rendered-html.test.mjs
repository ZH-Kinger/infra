import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("builds the dataset operations control plane instead of the starter", async () => {
  const [page, consoleSource, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/OpsConsole.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  const product = `${page}\n${consoleSource}\n${layout}`;
  assert.match(product, /Dataset Ops/i);
  assert.match(product, /DATASET OPS/);
  assert.match(product, /训练数据运维控制台/);
  assert.match(product, /存量纳管/);
  assert.match(product, /CPFS/);
  assert.doesNotMatch(product, /codex-preview|react-loading-skeleton|Your site is taking shape/i);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});

test("keeps write operations behind server-side workflow dispatch", async () => {
  const [route, page, hosting, schema] = await Promise.all([
    readFile(new URL("../app/api/operations/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/OpsConsole.tsx", import.meta.url), "utf8"),
    readFile(new URL("../.openai/hosting.json", import.meta.url), "utf8"),
    readFile(new URL("../db/schema.ts", import.meta.url), "utf8"),
  ]);

  assert.match(route, /OPS_ADMIN_EMAILS/);
  assert.match(route, /GITHUB_TOKEN/);
  assert.match(route, /dataset-release\.yml/);
  assert.match(route, /dataset-lifecycle\.yml/);
  assert.match(route, /pai-runtime\.yml/);
  assert.match(route, /if \(execute\)/);
  assert.match(page, /PLAN ONLY/);
  assert.match(hosting, /"d1": "DB"/);
  assert.match(schema, /idx_operations_dataset_created_at/);
});
