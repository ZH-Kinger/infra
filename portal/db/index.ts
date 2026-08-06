import { env } from "cloudflare:workers";
import { drizzle } from "drizzle-orm/d1";
import * as schema from "./schema";

export function getDb() {
  if (!env.DB) {
    throw new Error(
      "Cloudflare D1 binding `DB` is unavailable. Set the `d1` field in .openai/hosting.json to `DB` or let your control plane inject the real binding values before using the database."
    );
  }

  return drizzle(env.DB, { schema });
}

export async function ensureDb() {
  if (!env.DB) throw new Error("Cloudflare D1 binding `DB` is unavailable.");
  await env.DB.batch([
    env.DB.prepare(`CREATE TABLE IF NOT EXISTS operations (
      id TEXT PRIMARY KEY NOT NULL,
      kind TEXT NOT NULL,
      dataset TEXT DEFAULT '' NOT NULL,
      mode TEXT DEFAULT 'plan' NOT NULL,
      actor TEXT NOT NULL,
      status TEXT NOT NULL,
      workflow TEXT DEFAULT '' NOT NULL,
      payload TEXT NOT NULL,
      run_url TEXT DEFAULT '' NOT NULL,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL
    )`),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS idx_operations_created_at ON operations(created_at)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS idx_operations_dataset_created_at ON operations(dataset, created_at)"),
  ]);
  return getDb();
}
