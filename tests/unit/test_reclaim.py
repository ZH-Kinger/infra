from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from dataset_sink.reclaim import (
    KEEP_MARKER,
    TRASH_DIR,
    AssumeRecoverable,
    ReleaseInfo,
    execute_plan,
    plan_reclaim,
    scan_releases,
    sweep_trash,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


class RefuseAll:
    def recoverable(self, release: ReleaseInfo) -> Tuple[bool, str]:
        del release
        return False, "假装归档没了"


class InUseProbe:
    def __init__(self, *labels: str) -> None:
        self.labels = set(labels)

    def in_use(self, release: ReleaseInfo) -> Optional[str]:
        return "job dlc-123 正在挂载" if release.label in self.labels else None


def _make_root(tmp: Path, releases) -> Path:
    """releases: [(dataset, commit, age_days, size, ready, pinned)]"""
    root = tmp / "cpfs"
    (root / ".locks").mkdir(parents=True, exist_ok=True)
    (root / ".materializing").mkdir(parents=True, exist_ok=True)
    for dataset, commit, age_days, size, ready, pinned in releases:
        d = root / dataset / commit
        (d / "shards").mkdir(parents=True, exist_ok=True)
        (d / "shards" / "a.bin").write_bytes(b"x" * 8)
        created = (NOW - timedelta(days=age_days)).isoformat()
        (d / "release.json").write_text(
            json.dumps(
                {
                    "dataset": dataset,
                    "repository": "robotics-data",
                    "lakefs_commit": commit,
                    "manifest_sha256": "a" * 64,
                    "size_bytes": size,
                    "file_count": 1,
                    "created_at": created,
                    "release_path": str(d),
                }
            ),
            encoding="utf-8",
        )
        (d / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
        if ready:
            (d / "_READY").write_text("{}", encoding="utf-8")
        if pinned:
            (d / KEEP_MARKER).write_text("", encoding="utf-8")
    return root


class ScanTests(unittest.TestCase):
    def test_skips_protocol_directories(self):
        tmp = Path(tempfile.mkdtemp())
        root = _make_root(tmp, [("robotics", "c1", 30, 100, True, False)])
        (root / TRASH_DIR / "robotics").mkdir(parents=True)

        found = scan_releases(root)
        # .locks / .materializing / .trash 都不是数据集
        self.assertEqual([r.label for r in found], ["robotics/c1"])

    def test_reads_size_and_time_from_release_json(self):
        tmp = Path(tempfile.mkdtemp())
        root = _make_root(tmp, [("robotics", "c1", 30, 4096, True, False)])
        r = scan_releases(root)[0]
        self.assertEqual(r.size_bytes, 4096)
        self.assertEqual(r.repository, "robotics-data")
        self.assertTrue(r.ready)
        self.assertFalse(r.pinned)

    def test_unreadable_release_json_does_not_crash_scan(self):
        tmp = Path(tempfile.mkdtemp())
        root = _make_root(tmp, [("robotics", "c1", 30, 100, True, False)])
        (root / "robotics" / "c1" / "release.json").write_text("{ broken", encoding="utf-8")
        r = scan_releases(root)[0]
        # 元数据读不出来 → repository 为空 → 后面会因「无法确认可重建」而保留
        self.assertIsNone(r.repository)


class PlanTests(unittest.TestCase):
    def _plan(self, releases, **kw):
        tmp = Path(tempfile.mkdtemp())
        root = _make_root(tmp, releases)
        kw.setdefault("recoverability_probe", AssumeRecoverable())
        return root, plan_reclaim(scan_releases(root), now=NOW, **kw)

    def _reasons(self, plan):
        return {d.release.label: d.reason for d in plan.retain}

    def test_reclaims_old_releases_beyond_keep_last(self):
        _, plan = self._plan(
            [
                ("robotics", "c_old", 90, 100, True, False),
                ("robotics", "c_mid", 60, 100, True, False),
                ("robotics", "c_new1", 30, 100, True, False),
                ("robotics", "c_new2", 20, 100, True, False),
            ],
            min_age_days=14,
            keep_last=2,
        )
        self.assertEqual(
            [d.release.label for d in plan.reclaim], ["robotics/c_old", "robotics/c_mid"]
        )
        self.assertEqual(plan.reclaimable_bytes, 200)

    def test_keep_last_prevents_emptying_a_dataset(self):
        # 全部都很老，但 keep_last 仍然保住最近两个
        _, plan = self._plan(
            [
                ("robotics", "c1", 300, 100, True, False),
                ("robotics", "c2", 200, 100, True, False),
            ],
            min_age_days=14,
            keep_last=2,
        )
        self.assertEqual(plan.reclaim, ())

    def test_protection_window_holds_recent_releases(self):
        _, plan = self._plan(
            [
                ("robotics", "a", 100, 100, True, False),
                ("robotics", "b", 100, 100, True, False),
                ("robotics", "c", 3, 100, True, False),
            ],
            min_age_days=14,
            keep_last=0,
        )
        self.assertIn("未过 14 天保护期", self._reasons(plan)["robotics/c"])

    def test_keep_marker_pins_a_release(self):
        _, plan = self._plan(
            [("robotics", "old", 300, 100, True, True)],
            min_age_days=14,
            keep_last=0,
        )
        self.assertEqual(plan.reclaim, ())
        self.assertIn("人工置顶", self._reasons(plan)["robotics/old"])

    def test_incomplete_releases_are_left_alone_by_default(self):
        _, plan = self._plan(
            [("robotics", "half", 300, 100, False, False)],
            min_age_days=14,
            keep_last=0,
        )
        self.assertEqual(plan.reclaim, ())
        self.assertIn("_READY", self._reasons(plan)["robotics/half"])

        _, plan2 = self._plan(
            [("robotics", "half", 300, 100, False, False)],
            min_age_days=14,
            keep_last=0,
            include_incomplete=True,
        )
        self.assertEqual([d.release.label for d in plan2.reclaim], ["robotics/half"])

    def test_unrecoverable_releases_are_never_reclaimed(self):
        # 这是整个模块最重要的一条：不确认能重建就绝不删
        _, plan = self._plan(
            [("robotics", "old", 300, 100, True, False)],
            min_age_days=14,
            keep_last=0,
            recoverability_probe=RefuseAll(),
        )
        self.assertEqual(plan.reclaim, ())
        self.assertIn("删了无法重建", self._reasons(plan)["robotics/old"])

    def test_in_use_releases_are_retained(self):
        _, plan = self._plan(
            [("robotics", "old", 300, 100, True, False)],
            min_age_days=14,
            keep_last=0,
            usage_probe=InUseProbe("robotics/old"),
        )
        self.assertEqual(plan.reclaim, ())
        self.assertIn("正在使用中", self._reasons(plan)["robotics/old"])

    def test_reclaim_bytes_stops_once_target_is_met_oldest_first(self):
        _, plan = self._plan(
            [
                ("robotics", "c_oldest", 300, 100, True, False),
                ("robotics", "c_older", 200, 100, True, False),
                ("robotics", "c_old", 100, 100, True, False),
            ],
            min_age_days=14,
            keep_last=0,
            reclaim_bytes=150,
        )
        # 最旧的先删；删够 150 字节需要两个 100
        self.assertEqual(
            [d.release.label for d in plan.reclaim],
            ["robotics/c_oldest", "robotics/c_older"],
        )

    def test_keep_last_is_per_dataset_not_global(self):
        _, plan = self._plan(
            [
                ("a", "a1", 300, 100, True, False),
                ("a", "a2", 200, 100, True, False),
                ("b", "b1", 300, 100, True, False),
            ],
            min_age_days=14,
            keep_last=1,
        )
        # a 留下最新的 a2，淘汰 a1；b 只有一个版本，被 keep_last 保住
        self.assertEqual([d.release.label for d in plan.reclaim], ["a/a1"])

    def test_requires_an_explicit_recoverability_probe(self):
        with self.assertRaises(ValueError):
            plan_reclaim([], now=NOW)


class ExecuteTests(unittest.TestCase):
    def _setup(self, **kw):
        tmp = Path(tempfile.mkdtemp())
        root = _make_root(
            tmp,
            [
                ("robotics", "c_old", 300, 100, True, False),
                ("robotics", "c_new", 1, 100, True, False),
            ],
        )
        kw.setdefault("min_age_days", 14)
        kw.setdefault("keep_last", 0)
        plan = plan_reclaim(
            scan_releases(root), now=NOW, recoverability_probe=AssumeRecoverable(), **kw
        )
        return root, plan

    def test_dry_run_touches_nothing(self):
        root, plan = self._setup()
        result = execute_plan(plan, root, execute=False)

        self.assertEqual(result.deleted, ("robotics/c_old",))
        self.assertFalse(result.executed)
        self.assertTrue((root / "robotics" / "c_old").is_dir())  # 还在

    def test_execute_removes_the_directory(self):
        root, plan = self._setup()
        result = execute_plan(plan, root, execute=True)

        self.assertEqual(result.deleted, ("robotics/c_old",))
        self.assertFalse((root / "robotics" / "c_old").exists())
        self.assertTrue((root / "robotics" / "c_new").is_dir())  # 保护期内的没动
        self.assertEqual(result.freed_bytes, 100)

    def test_keep_marker_added_after_planning_still_wins(self):
        # 计划是在锁外生成的，执行前有人置顶了它 → 必须放弃删除
        root, plan = self._setup()
        (root / "robotics" / "c_old" / KEEP_MARKER).write_text("", encoding="utf-8")

        result = execute_plan(plan, root, execute=True)
        self.assertEqual(result.deleted, ())
        self.assertEqual([label for label, _ in result.skipped], ["robotics/c_old"])
        self.assertTrue((root / "robotics" / "c_old").is_dir())

    def test_already_gone_release_is_skipped_not_fatal(self):
        root, plan = self._setup()
        import shutil

        shutil.rmtree(root / "robotics" / "c_old")

        result = execute_plan(plan, root, execute=True)
        self.assertEqual([why for _, why in result.skipped], ["已经不存在"])

    def test_trash_sweep_clears_leftovers(self):
        root, _ = self._setup()
        grave = root / TRASH_DIR / "robotics" / "leftover"
        grave.mkdir(parents=True)
        (grave / "junk").write_text("x", encoding="utf-8")

        count, swept = sweep_trash(root, execute=False)
        self.assertEqual((count, swept), (1, 0))
        self.assertTrue(grave.exists())

        count, swept = sweep_trash(root, execute=True)
        self.assertEqual((count, swept), (1, 1))
        self.assertFalse(grave.exists())


if __name__ == "__main__":
    unittest.main()
