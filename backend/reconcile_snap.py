"""乙路径（不实现）：reconcile_snap 世代快照物化复检。

世代对账册当天已钉死甲路径；本模块刻意留空，任何调用即视为偏离口径，
由 reconcile_svc 拒绝执行并记「对账偏了」，防止两条路径混用。
"""
from __future__ import annotations


def snapshot_reconcile(conn, window_id: int, generation: int) -> None:
    raise NotImplementedError(
        "乙路径未实现：本仓口径为甲路径（世代过滤查询），不得混用"
    )
