import { sql } from "drizzle-orm";
import { index, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const operations = sqliteTable(
  "operations",
  {
    id: text("id").primaryKey(),
    kind: text("kind").notNull(),
    dataset: text("dataset").notNull().default(""),
    mode: text("mode").notNull().default("plan"),
    actor: text("actor").notNull(),
    status: text("status").notNull(),
    workflow: text("workflow").notNull().default(""),
    payload: text("payload").notNull(),
    runUrl: text("run_url").notNull().default(""),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    index("idx_operations_created_at").on(table.createdAt),
    index("idx_operations_dataset_created_at").on(table.dataset, table.createdAt),
  ],
);
