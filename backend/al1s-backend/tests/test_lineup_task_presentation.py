import pytest

from al1s.lineup.task_presentation import batch_history_summary, lineup_task_name


@pytest.mark.parametrize(
    ("lifecycle", "success", "running", "queued", "status", "result"),
    [
        ("active", 0, 0, 0, "waiting", None),
        ("active", 1, 0, 0, "waiting", None),
        ("active", 1, 0, 1, "queued", None),
        ("active", 1, 1, 0, "running", None),
        ("completed", 2, 0, 0, "ended", "success"),
        ("completed", 1, 0, 0, "ended", "failure"),
        ("completed", 0, 0, 0, "ended", "failure"),
        ("cancelled", 1, 0, 0, "cancelled", None),
    ],
)
def test_entire_batch_status(lifecycle, success, running, queued, status, result):
    summary = batch_history_summary(lifecycle, 2, success, running, queued)
    assert summary.image_count == 2
    assert summary.execution_status == status
    assert summary.result == result


def test_names_only_contain_the_count():
    assert lineup_task_name(1) == "阵容识别 · 1 张"
    assert lineup_task_name(43) == "阵容识别 · 43 张"
