"""分区放行领域服务：列表/登记骨架复用基类，真实放行与凭据状态机由 PartitionGateManager 承载。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class PartitionGateService(BaseModuleService):
    MODULE = "partition_gate"
    ENTITY = "分区放行"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "放行编号", "融合批次", "目标宽表", "分区编号", "分区版本",
        "确认结果", "阻断规则", "告警规则", "关联凭据",
    ]
    REQUIRED_FIELDS = ["放行编号", "融合批次", "目标宽表", "分区编号"]
    STATUS_ORDER = ["放行", "待核", "已确认", "已回退"]
    ACTION_RULES = {"分区校验": "放行", "标记待核": "待核", "确认凭据": "已确认"}
    NEGATIVE_ACTIONS = ["标记待核"]

    def release_rows(self) -> list[dict[str, Any]]:
        """把引擎分区放行状态机的内容投影成资源列表行。"""
        from app.fusion_engine import fusion_engine

        rows: list[dict[str, Any]] = []
        for index, release in enumerate(fusion_engine.partition_gate.list_releases(), start=1):
            for detail in release.readable_partition_details():
                rows.append({
                    "id": index,
                    "status": detail["decision"],
                    "pending": detail["decision"] != "放行",
                    "abnormal": detail["decision"] != "放行",
                    "放行编号": detail["release_code"],
                    "融合批次": release.batch_code,
                    "目标宽表": release.target_wide_table,
                    "分区编号": detail["partition"],
                    "分区版本": detail["partition_version"],
                    "确认结果": detail["decision"],
                    "阻断规则": "、".join(detail["blocking_rules"]) or "无",
                    "告警规则": "、".join(detail["warning_rules"]) or "无",
                    "关联凭据": detail["credential_code"] or "未关联",
                })
        return rows

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 种子示例行 + 引擎状态机实时行，实时行 id 接在种子之后避免主键冲突。
        seed_rows = [dict(row) for row in store.rows(self.MODULE)]
        live_rows = self.release_rows()
        base_id = max((int(row.get("id", 0)) for row in seed_rows), default=0)
        for offset, row in enumerate(live_rows, start=1):
            row = dict(row)
            row["id"] = base_id + offset
            seed_rows.append(row)
        rows = seed_rows
        for field_name, expected in (filters or {}).items():
            if field_name in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field_name, "")).casefold()
                ]
        if keyword:
            rows = [
                row for row in rows
                if keyword.casefold() in str(row.get("放行编号", "")).casefold()
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total
