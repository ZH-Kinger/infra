CREATE TABLE `operations` (
	`id` text PRIMARY KEY NOT NULL,
	`kind` text NOT NULL,
	`dataset` text DEFAULT '' NOT NULL,
	`mode` text DEFAULT 'plan' NOT NULL,
	`actor` text NOT NULL,
	`status` text NOT NULL,
	`workflow` text DEFAULT '' NOT NULL,
	`payload` text NOT NULL,
	`run_url` text DEFAULT '' NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_operations_created_at` ON `operations` (`created_at`);--> statement-breakpoint
CREATE INDEX `idx_operations_dataset_created_at` ON `operations` (`dataset`,`created_at`);