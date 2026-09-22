"""数据类型词表：所有桶的一级目录只能从这里取，新增要审批。

规范（docs/collab/planning/oss-storage-spec.md）的三条规则都压在它身上：
一级目录共用一张词表、每种类型的层级在所有桶里一模一样、新增走审批。
这里盯的是会**静默**出错的那几处：层级不一致、保留名被拿去当类型、
审批通过后写坏了整张表。
"""

import json
import tempfile
import unittest
from pathlib import Path

from delivery import catalog as catalog_mod
from delivery import datatypes
from delivery.flows import FlowError, Flows

TABLE = {
    "types": {
        "third-party-data": {"label": "供应商数据", "layers": ["供应商", "批次ID"]},
        "public-datasets": {"label": "开源数据集", "layers": ["数据集名"]},
        "label": {"label": "标注成品", "layers": ["来源", "版本", "批次ID"]},
        "internet": {"label": "互联网数据", "layers": ["来源", "批次ID"]},
    },
    "buckets": {
        "wuji-raw": ["third-party-data", "public-datasets", "internet"],
        "wuji-processed": ["label"],
    },
}


class ParseTests(unittest.TestCase):
    def test_a_good_table_loads(self):
        reg = datatypes.parse(TABLE)
        self.assertEqual(reg.get("label")["layers"], ["来源", "版本", "批次ID"])
        self.assertEqual(reg.for_bucket("wuji-processed"), ("label",))

    def test_reserved_names_cannot_be_types(self):
        """`tmp/` 有 7 天清理规则。拿 tmp 当数据类型，那一类数据会被一起删掉。"""
        for name in ("tmp", "general", "staging", "qc"):
            bad = {"types": {name: {"label": "x", "layers": ["批次ID"]}}}
            with self.assertRaises(datatypes.DataTypeError, msg=name):
                datatypes.parse(bad)

    def test_the_batch_layer_can_only_be_last(self):
        with self.assertRaises(datatypes.DataTypeError):
            datatypes.check_layers(["批次ID", "来源"])

    def test_too_deep_is_refused(self):
        """再深就没人记得住该放哪了 —— 然后大家就往桶根下随手建目录。"""
        with self.assertRaises(datatypes.DataTypeError):
            datatypes.check_layers(["a", "b", "c", "d", "批次ID"])

    def test_a_bucket_cannot_list_a_type_that_is_not_defined(self):
        bad = {"types": {}, "buckets": {"wuji-raw": ["nope"]}}
        with self.assertRaises(datatypes.DataTypeError):
            datatypes.parse(bad)


class AppendTests(unittest.TestCase):
    def _file(self):
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        path = Path(box.name) / datatypes.FILENAME
        path.write_text(json.dumps(TABLE, ensure_ascii=False), encoding="utf-8")
        return path

    def test_a_new_type_lands_in_the_table_and_in_the_chosen_buckets(self):
        path = self._file()
        datatypes.append(
            str(path),
            key="sim-data",
            label="仿真数据",
            layers=["来源", "批次ID"],
            buckets=["wuji-raw"],
        )
        reg = datatypes.load(str(path))
        self.assertEqual(reg.get("sim-data")["label"], "仿真数据")
        self.assertIn("sim-data", reg.for_bucket("wuji-raw"))
        self.assertNotIn("sim-data", reg.for_bucket("wuji-processed"))

    def test_an_existing_type_is_never_overwritten(self):
        """覆盖会悄悄改掉一个在用类型的层级，而已经按旧层级建好的目录不会跟着变。"""
        path = self._file()
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(datatypes.DataTypeError):
            datatypes.append(
                str(path), key="label", label="x", layers=["批次ID"], buckets=["wuji-raw"]
            )
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_a_write_that_would_break_the_table_leaves_it_untouched(self):
        """写了一半的 JSON 会让所有模板加载失败、整个申请页打不开。校验在写之前。"""
        path = self._file()
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(datatypes.DataTypeError):
            datatypes.append(
                str(path), key="tmp", label="x", layers=["批次ID"], buckets=["wuji-raw"]
            )
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        left = sorted(p.name for p in path.parent.iterdir() if not p.name.endswith(".lock"))
        self.assertEqual(left, [path.name], "临时文件没清掉")

    def test_a_retry_after_the_write_already_landed_is_a_success(self):
        """上一次写进去了、只是单子没来得及落盘。重试判失败的话，单子显示「失败」，
        而那个类型其实已经在词表里了（审计 L-1）。"""
        path = self._file()
        args = dict(
            key="sim-data", label="仿真数据", layers=["来源", "批次ID"], buckets=["wuji-raw"]
        )
        datatypes.append(str(path), **args)
        datatypes.append(str(path), **args)  # 不该抛
        with self.assertRaises(datatypes.DataTypeError):
            datatypes.append(str(path), **dict(args, layers=["批次ID"]))

    def test_two_writers_do_not_lose_each_other(self):
        """审批回调和 sweep 是两个进程。不加锁的话两边各读旧表、各加一个，
        后写的覆盖先写的 —— 两张单子都记成「已加入」，其中一个类型却没了（审计 M-2）。"""
        import threading

        path = self._file()
        keys = [f"type-{i}" for i in range(8)]

        def add(k):
            datatypes.append(str(path), key=k, label=k, layers=["批次ID"], buckets=["wuji-raw"])

        workers = [threading.Thread(target=add, args=(k,)) for k in keys]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        got = datatypes.load(str(path))
        self.assertEqual(sorted(k for k in got.types if k.startswith("type-")), sorted(keys))

    def test_the_note_is_kept(self):
        """申请里写的用途要跟进词表，不然半年后没人知道这个类型是干什么的（审计 L-2）。"""
        path = self._file()
        datatypes.append(
            str(path),
            key="sim-data",
            label="仿真数据",
            layers=["批次ID"],
            buckets=["wuji-raw"],
            note="仿真器导出的轨迹",
        )
        self.assertEqual(datatypes.load(str(path)).get("sim-data")["note"], "仿真器导出的轨迹")


STORAGE_TPL = {
    "id": "oss-dir",
    "kind": "storage",
    "platform": "aliyun",
    "account": "170406579653",
    "title": "新建数据目录",
    "buckets": [
        {"name": "wuji-raw", "region": "cn-hangzhou"},
        {"name": "wuji-processed", "region": "cn-hangzhou"},
    ],
}
DATATYPE_TPL = {
    "id": "data-type-new",
    "kind": "datatype",
    "platform": "aliyun",
    "account": "170406579653",
    "title": "新增数据类型",
    "buckets": [
        {"name": "wuji-raw", "region": "cn-hangzhou"},
        {"name": "wuji-processed", "region": "cn-hangzhou"},
    ],
}


class CatalogTests(unittest.TestCase):
    def test_a_storage_template_without_stages_takes_them_from_the_table(self):
        """分类只在词表里维护一份。模板里再抄一份的话，词表加了新类型、模板没跟着改，
        申请页上就选不到它，而没有任何报错提醒这件事。"""
        tpl = catalog_mod.parse_template(STORAGE_TPL, 0, None, datatypes.parse(TABLE))
        self.assertEqual(
            set(tpl.stages["wuji-raw"]), {"third-party-data", "public-datasets", "internet"}
        )
        self.assertEqual(tpl.types["label"]["layers"], ["来源", "版本", "批次ID"])

    def test_a_bucket_the_table_says_nothing_about_is_an_error(self):
        spec = dict(STORAGE_TPL, buckets=[{"name": "wuji-other", "region": "cn-hangzhou"}])
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse_template(spec, 0, None, datatypes.parse(TABLE))

    def test_the_datatype_template_needs_the_table(self):
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse_template(DATATYPE_TPL, 0, None, datatypes.Registry())

    def test_the_frontend_gets_names_and_layers(self):
        tpl = catalog_mod.parse_template(STORAGE_TPL, 0, None, datatypes.parse(TABLE))
        shown = tpl.public()["types"]
        self.assertEqual(
            shown["label"], {"label": "标注成品", "layers": ["来源", "版本", "批次ID"]}
        )


def _flows(tpl_spec, *, path=""):
    reg = datatypes.parse(TABLE)
    tpl = catalog_mod.parse_template(tpl_spec, 0, None, reg)
    f = Flows.__new__(Flows)
    cat = catalog_mod.Catalog((tpl,), datatypes=reg, datatypes_path=path)
    f._catalog = lambda: cat
    return f, tpl


class StorageRequestTests(unittest.TestCase):
    def _submit(self, **payload):
        f, tpl = _flows(STORAGE_TPL)
        return f._validate_storage(tpl, payload, "x")

    def test_every_layer_the_table_lists_is_asked_for(self):
        clean, _ = self._submit(
            bucket="wuji-processed", stage="label", segments=["shutu", "v2", "20260922-kitchen"]
        )
        self.assertEqual(clean["path"], "wuji-processed/label/shutu/v2/20260922-kitchen/")

    def test_a_missing_or_extra_layer_is_refused(self):
        """少一层或多一层，同一个类型在不同桶里深度就不一样了 —— 「只换桶名」不再成立。"""
        for segs in (["shutu", "20260922-kitchen"], ["a", "b", "c", "d"]):
            with self.assertRaises(FlowError, msg=str(segs)):
                self._submit(bucket="wuji-processed", stage="label", segments=segs)

    def test_a_segment_cannot_climb_out_of_its_layer(self):
        """`a/../b` 能让路径跳出它该在的那一层，而这串会进 OSS key 和 RAM 策略。"""
        for bad in ("..", "a/b", "a*", " ", ""):
            with self.assertRaises(FlowError, msg=bad):
                self._submit(
                    bucket="wuji-processed", stage="label", segments=["shutu", bad, "20260922-x"]
                )

    def test_a_type_with_no_batch_layer_works(self):
        clean, _ = self._submit(
            bucket="wuji-raw", stage="public-datasets", segments=["egodex"], license="CC-BY-4.0"
        )
        self.assertEqual(clean["path"], "wuji-raw/public-datasets/egodex/")

    def test_internet_and_open_data_still_need_a_license(self):
        """出合规问题时那是唯一能自证的东西 —— 换了新名字不能把这道闸丢了。"""
        with self.assertRaises(FlowError):
            self._submit(bucket="wuji-raw", stage="internet", segments=["youtube", "20260922-a"])

    def test_a_type_not_allowed_in_that_bucket_is_refused(self):
        with self.assertRaises(FlowError):
            self._submit(bucket="wuji-raw", stage="label", segments=["a", "b", "c"])


class DatatypeRequestTests(unittest.TestCase):
    def test_a_good_request_passes(self):
        f, tpl = _flows(DATATYPE_TPL)
        clean, summary = f._validate_datatype(
            tpl,
            {
                "key": "sim-data",
                "label": "仿真数据",
                "layers": ["来源", "批次ID"],
                "buckets": ["wuji-raw"],
            },
            "x",
        )
        self.assertEqual(clean["key"], "sim-data")
        self.assertIn("sim-data/<来源>/<批次ID>/", summary)

    def test_an_existing_key_is_refused_before_it_goes_to_approval(self):
        """审批通过后才发现重名，这张单子就只能失败，白走一轮审批。"""
        f, tpl = _flows(DATATYPE_TPL)
        with self.assertRaises(FlowError):
            f._validate_datatype(
                tpl,
                {"key": "label", "label": "x", "layers": ["批次ID"], "buckets": ["wuji-raw"]},
                "x",
            )

    def test_a_reserved_name_is_refused(self):
        f, tpl = _flows(DATATYPE_TPL)
        with self.assertRaises(FlowError):
            f._validate_datatype(
                tpl,
                {"key": "tmp", "label": "x", "layers": ["批次ID"], "buckets": ["wuji-raw"]},
                "x",
            )

    def test_buckets_outside_the_template_are_dropped_and_none_left_is_refused(self):
        f, tpl = _flows(DATATYPE_TPL)
        with self.assertRaises(FlowError):
            f._validate_datatype(
                tpl,
                {
                    "key": "sim-data",
                    "label": "x",
                    "layers": ["批次ID"],
                    "buckets": ["someone-else"],
                },
                "x",
            )

    def test_approval_writes_the_type_into_the_table(self):
        """审批通过就写进词表，不停在「待开通」—— 这一步没有需要人去云上做的事。"""
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        path = Path(box.name) / datatypes.FILENAME
        path.write_text(json.dumps(TABLE, ensure_ascii=False), encoding="utf-8")
        f, tpl = _flows(DATATYPE_TPL, path=str(path))
        payload = {
            "key": "sim-data",
            "label": "仿真数据",
            "layers": ["来源", "批次ID"],
            "buckets": ["wuji-raw"],
            "note": "",
        }
        got = f._run(tpl, {"id": "REQ-1", "payload": payload})
        self.assertIn("sim-data", got)
        self.assertIn("sim-data", datatypes.load(str(path)).for_bucket("wuji-raw"))

    def test_the_datatype_request_is_not_parked_waiting_for_a_person(self):
        self.assertNotIn(catalog_mod.KIND_DATATYPE, catalog_mod.AWAIT_FULFIL)


if __name__ == "__main__":
    unittest.main()


class ServerCacheTests(unittest.TestCase):
    """服务进程里的模板缓存要跟着词表一起失效（审计 M-1）。

    只看模板文件修改时间的话：「新增数据类型」批下来、词表写好了，缓存却不动 ——
    申请页选不到新类型，查重还用旧表，而单子上写着「现在能选它了」。
    """

    def test_a_new_type_shows_up_without_a_restart(self):
        import os

        from delivery.server import Backend

        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        root = Path(box.name)
        tpl = root / "request-templates.json"
        tpl.write_text(
            json.dumps(
                {"schema": catalog_mod.SCHEMA, "templates": [STORAGE_TPL]}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        types = root / datatypes.FILENAME
        types.write_text(json.dumps(TABLE, ensure_ascii=False), encoding="utf-8")

        backend = Backend(templates_path=str(tpl))
        before = backend.catalog().get("oss-dir").stages["wuji-raw"]
        self.assertNotIn("sim-data", before)

        datatypes.append(
            str(types),
            key="sim-data",
            label="仿真数据",
            layers=["来源", "批次ID"],
            buckets=["wuji-raw"],
        )
        # 同一纳秒内写两次时修改时间可能不变，推一下，别让测试依赖文件系统的时间精度
        st = types.stat()
        os.utime(types, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        after = backend.catalog().get("oss-dir").stages["wuji-raw"]
        self.assertIn("sim-data", after)
