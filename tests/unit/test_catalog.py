from __future__ import annotations

import unittest

from dataset_sink.catalog import resolve_pai_dataset_id
from dataset_sink.errors import DatasetSinkError


class PaiDatasetCatalogTests(unittest.TestCase):
    def test_resolves_existing_dataset_by_short_name(self):
        mapping = '{"robotics":"d-robotics","vision":"d-vision"}'
        self.assertEqual(resolve_pai_dataset_id("robotics", mapping), "d-robotics")

    def test_unknown_dataset_fails_closed_and_lists_known_names(self):
        with self.assertRaisesRegex(DatasetSinkError, "已配置：robotics"):
            resolve_pai_dataset_id("unknown", '{"robotics":"d-robotics"}')

    def test_legacy_single_dataset_variable_remains_supported(self):
        self.assertEqual(
            resolve_pai_dataset_id("robotics", "", fallback="d-legacy"),
            "d-legacy",
        )

    def test_mapping_must_be_an_object(self):
        with self.assertRaisesRegex(DatasetSinkError, "必须是 JSON object"):
            resolve_pai_dataset_id("robotics", '["d-robotics"]')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
