from unittest.mock import patch

from delivery import assets


def test_workspace_choices_show_named_resource_spaces_and_drop_empty_ones():
    with (
        patch.object(
            assets,
            "_pai_page",
            return_value=[
                {"WorkspaceId": "2", "WorkspaceName": "empty"},
                {"WorkspaceId": "1", "WorkspaceName": "ai_hz_gpu"},
            ],
        ),
        patch.object(
            assets,
            "quotas_by_workspace",
            return_value=({"1": {"gpu": 144, "quotas": ["gpu×144卡"]}}, []),
        ),
    ):
        got = assets.workspace_choices(object(), "cn-hangzhou")

    assert [x["id"] for x in got] == ["1"]
    assert got[0]["name"] == "ai_hz_gpu"
    assert got[0]["gpu"] == 144


def test_workspace_choices_keep_unknown_resource_status():
    with (
        patch.object(
            assets, "_pai_page", return_value=[{"WorkspaceId": "1", "WorkspaceName": "gpu"}]
        ),
        patch.object(assets, "quotas_by_workspace", return_value=({}, ["cn-hangzhou：无权限"])),
    ):
        assert (
            assets.workspace_choices(object(), "cn-hangzhou")[0]["resource_state"]
            == assets.CARDS_UNKNOWN
        )
