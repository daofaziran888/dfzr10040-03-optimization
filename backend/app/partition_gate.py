"""质量门禁分区放行与跨日补货取数凭据领域。

解决三个问题：
1. 规则按级别（阻断 / 告警）逐分区校验，告警只记录不阻断，避免被误作阻断；
2. 检查结果绑定实际覆盖的分区版本，同一版本重跑覆盖旧结果，不重复累计；
3. 分区放行结果与跨日补货取数凭据关联，待核分区不得替换已发布内容、
   不得冒充本期新数据，未发本期凭据时回退上个已确认窗口并留存等待原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# 归一化为阻断级别的取值；其余一律按告警处理。
BLOCK_LEVEL_TOKENS = {"block", "阻断", "critical", "error", "fatal"}

DECISION_RELEASED = "放行"
DECISION_PENDING = "待核"

WINDOW_CURRENT = "current"
WINDOW_PREVIOUS = "previous_confirmed"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_block_level(level: str) -> bool:
    """规则级别归一化：命中阻断词表才算阻断级，缺省按告警处理。"""
    return str(level or "").strip().lower() in BLOCK_LEVEL_TOKENS


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass
class RuleFinding:
    """单条规则在单个分区版本上的一次检查结果。

    告警及其触发规则必须跟着实际检查覆盖的分区版本走，因此分区编号、
    分区版本、规则编号和本次实际检查行数都固化在 finding 上。
    """

    rule_code: str
    check_type: str
    block_level: str
    level: str  # 归一化级别：阻断 / 告警
    partition: str
    partition_version: str
    checked_rows: int
    detail: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    triggered_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "check_type": self.check_type,
            "block_level": self.block_level,
            "level": self.level,
            "partition": self.partition,
            "partition_version": self.partition_version,
            "checked_rows": self.checked_rows,
            "detail": dict(self.detail),
            "message": self.message,
            "triggered_at": self.triggered_at,
        }


@dataclass
class PartitionCheckResult:
    """一个分区在一个融合批次中的检查结论。"""

    partition: str
    partition_version: str
    batch_code: str
    checked_rows: int
    decision: str  # 放行 / 待核
    findings: list[RuleFinding] = field(default_factory=list)
    checked_at: str = field(default_factory=now_iso)

    @property
    def warnings(self) -> list[RuleFinding]:
        return [f for f in self.findings if f.level != "阻断"]

    @property
    def blockers(self) -> list[RuleFinding]:
        return [f for f in self.findings if f.level == "阻断"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition": self.partition,
            "partition_version": self.partition_version,
            "batch_code": self.batch_code,
            "checked_rows": self.checked_rows,
            "decision": self.decision,
            "warnings": [f.to_dict() for f in self.warnings],
            "blockers": [f.to_dict() for f in self.blockers],
            "findings": [f.to_dict() for f in self.findings],
            "checked_at": self.checked_at,
        }


@dataclass
class BatchReleaseResult:
    """同一融合批次的分区放行汇总，凭据通过 release_code 与之关联。"""

    release_code: str
    batch_code: str
    target_wide_table: str
    partitions: dict[str, PartitionCheckResult] = field(default_factory=dict)
    credential_code: str | None = None
    created_at: str = field(default_factory=now_iso)

    @property
    def released_partitions(self) -> list[str]:
        return [p for p, r in self.partitions.items() if r.decision == DECISION_RELEASED]

    @property
    def blocked_partitions(self) -> list[str]:
        return [p for p, r in self.partitions.items() if r.decision == DECISION_PENDING]

    @property
    def all_released(self) -> bool:
        return bool(self.partitions) and not self.blocked_partitions

    def readable_partition_details(self) -> list[dict[str, Any]]:
        """跨日快照的可读分区明细：逐分区注明确认结果与关联凭据。"""
        details: list[dict[str, Any]] = []
        for partition, result in self.partitions.items():
            details.append({
                "partition": partition,
                "partition_version": result.partition_version,
                "checked_rows": result.checked_rows,
                "decision": result.decision,
                "confirmed": result.decision == DECISION_RELEASED,
                "warning_rules": [f.rule_code for f in result.warnings],
                "blocking_rules": [f.rule_code for f in result.blockers],
                "warnings": [f.to_dict() for f in result.warnings],
                "blockers": [f.to_dict() for f in result.blockers],
                "release_code": self.release_code,
                "credential_code": self.credential_code,
                "checked_at": result.checked_at,
            })
        return details

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_code": self.release_code,
            "batch_code": self.batch_code,
            "target_wide_table": self.target_wide_table,
            "decision": "分区放行" if self.all_released else "阻断待核",
            "released_partitions": self.released_partitions,
            "blocked_partitions": self.blocked_partitions,
            "credential_code": self.credential_code,
            "partition_details": self.readable_partition_details(),
            "created_at": self.created_at,
        }


@dataclass
class DataCredential:
    """跨日补货取数凭据：每个夜班请求只确认一次。"""

    credential_code: str
    request_id: str
    batch_code: str
    window_type: str  # current / previous_confirmed
    data_date: str
    amount: float
    totals: dict[str, float]
    release_code: str | None
    basis: str
    confirmed: bool = True
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_code": self.credential_code,
            "request_id": self.request_id,
            "batch_code": self.batch_code,
            "window_type": self.window_type,
            "data_date": self.data_date,
            "amount": self.amount,
            "totals": dict(self.totals),
            "release_code": self.release_code,
            "basis": self.basis,
            "confirmed": self.confirmed,
            "created_at": self.created_at,
        }


@dataclass
class ReplenishmentOrder:
    """按已确认凭据的日期和金额下达的跨日补货订单。"""

    request_id: str
    credential_code: str
    order_date: str
    amount: float
    placed_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "credential_code": self.credential_code,
            "order_date": self.order_date,
            "amount": self.amount,
            "placed_at": self.placed_at,
        }


@dataclass
class ConfirmedWindow:
    """已确认窗口合计（夜班兜底口径）。"""

    window_date: str
    total_amount: float
    totals: dict[str, float]
    confirmed_at: str = field(default_factory=now_iso)


class PartitionGateManager:
    """分区放行、发布保护、取数凭据与签出核对的状态机。"""

    def __init__(self) -> None:
        # batch_code -> 最新放行结果（重跑同批次覆盖，不累计）
        self._releases: dict[str, BatchReleaseResult] = {}
        # (table, partition) -> 已发布分区版本
        self._published: dict[tuple[str, str], dict[str, Any]] = {}
        # request_id -> 凭据（一请求一确认）
        self._credentials: dict[str, DataCredential] = {}
        self._orders: dict[str, ReplenishmentOrder] = {}
        # request_id -> 等待原因列表
        self._wait_reasons: dict[str, list[dict[str, Any]]] = {}
        # 已确认窗口，按日期排序保留
        self._confirmed_windows: list[ConfirmedWindow] = []
        # request_id -> 签出记录
        self._checkouts: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0

    def _next_code(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    # ---- 规则评估 ----

    def evaluate_rule(self, rule: Any, rows: list[dict[str, Any]],
                      partition: str, partition_version: str) -> RuleFinding | None:
        """按规则级别评估单条规则；未触发返回 None。

        只统计本次实际传入的分区版本行，checked_rows 以本次为准，不累加。
        """
        block = is_block_level(getattr(rule, "block_level", "warning"))
        level = "阻断" if block else "告警"
        check_type = rule.check_type
        threshold = rule.threshold_condition
        checked_rows = len(rows)
        detail: dict[str, Any] = {}
        message = ""
        triggered = False

        if check_type == "row_count_min":
            minimum = int(threshold)
            detail = {"actual": checked_rows, "threshold": minimum}
            if checked_rows < minimum:
                triggered = True
                message = (
                    f"分区 {partition} 行数 {checked_rows} 低于阈值 {minimum}"
                )
        elif check_type in ("pk_not_null", "null_check"):
            field_name = str(threshold or "id")
            null_rows = [
                idx for idx, rec in enumerate(rows)
                if _is_blank(rec.get(field_name))
            ]
            detail = {
                "field": field_name,
                "null_count": len(null_rows),
                "null_row_indexes": null_rows,
            }
            if null_rows:
                triggered = True
                label = "主键完整性" if check_type == "pk_not_null" else "非空"
                message = (
                    f"分区 {partition} {label}检查发现 {len(null_rows)} 行 "
                    f"字段 {field_name} 为空"
                )
        else:
            # 未知检查类型不产生结论，避免误阻断。
            return None

        if not triggered:
            return None
        return RuleFinding(
            rule_code=rule.rule_code,
            check_type=check_type,
            block_level=rule.block_level,
            level=level,
            partition=partition,
            partition_version=partition_version,
            checked_rows=checked_rows,
            detail=detail,
            message=message,
        )

    def record_batch_check(
        self,
        batch_code: str,
        target_wide_table: str,
        partition_results: dict[str, PartitionCheckResult],
    ) -> BatchReleaseResult:
        """登记一次批次分区检查；同批次重跑直接覆盖旧结果。"""
        previous = self._releases.get(batch_code)
        credential_code = previous.credential_code if previous else None
        result = BatchReleaseResult(
            release_code=previous.release_code if previous else self._next_code("REL"),
            batch_code=batch_code,
            target_wide_table=target_wide_table,
            partitions=dict(partition_results),
            credential_code=credential_code,
        )
        self._releases[batch_code] = result
        return result

    def get_release(self, batch_code: str) -> BatchReleaseResult | None:
        return self._releases.get(batch_code)

    def list_releases(self) -> list[BatchReleaseResult]:
        return list(self._releases.values())

    # ---- 已发布内容保护 ----

    def publish_partition(self, batch_code: str, partition: str) -> dict[str, Any]:
        """放行分区才能发布；待核分区不得用本次数据替换已发布内容。"""
        release = self._releases.get(batch_code)
        if release is None:
            return {"ok": False, "message": f"批次 {batch_code} 尚无分区放行结果"}
        result = release.partitions.get(partition)
        if result is None:
            return {"ok": False, "message": f"批次未覆盖分区 {partition}"}
        if result.decision != DECISION_RELEASED:
            current = self._published.get((release.target_wide_table, partition))
            return {
                "ok": False,
                "message": (
                    f"分区 {partition} 存在阻断问题（{[f.rule_code for f in result.blockers]}），"
                    "不能用本次数据替换已发布内容"
                ),
                "decision": result.decision,
                "current_published_version": current.get("partition_version") if current else None,
            }

        payload = {
            "partition_version": result.partition_version,
            "record_count": result.checked_rows,
            "release_code": release.release_code,
            "published_at": now_iso(),
            "batch_code": batch_code,
        }
        self._published[(release.target_wide_table, partition)] = payload
        return {"ok": True, "partition": partition, **payload}

    def get_published(self, target_wide_table: str, partition: str) -> dict[str, Any] | None:
        return self._published.get((target_wide_table, partition))

    # ---- 已确认窗口 ----

    def confirm_window(self, window_date: str, total_amount: float,
                       totals: dict[str, float] | None = None) -> ConfirmedWindow:
        """登记一个已确认窗口合计；同日期覆盖为最新确认值。"""
        self._confirmed_windows = [
            w for w in self._confirmed_windows if w.window_date != window_date
        ]
        window = ConfirmedWindow(
            window_date=window_date,
            total_amount=float(total_amount),
            totals=dict(totals or {}),
        )
        self._confirmed_windows.append(window)
        self._confirmed_windows.sort(key=lambda w: w.window_date)
        return window

    def latest_confirmed_window(self) -> ConfirmedWindow | None:
        return self._confirmed_windows[-1] if self._confirmed_windows else None

    # ---- 取数凭据 ----

    def issue_credential(self, request_id: str, batch_code: str,
                         current_totals: dict[str, float],
                         current_date: str) -> dict[str, Any]:
        """为夜班跨日补货请求确认取数凭据。

        每个 request_id 只接受一次确认；批次含待核分区时不发本期凭据，
        记录等待原因并回退上个已确认窗口合计。
        """
        if request_id in self._credentials:
            existing = self._credentials[request_id]
            return {
                "ok": False,
                "message": f"补货请求 {request_id} 已确认凭据 {existing.credential_code}，不再重复受理",
                "credential": existing.to_dict(),
            }

        release = self._releases.get(batch_code)
        if release is None:
            return {"ok": False, "message": f"批次 {batch_code} 尚无分区放行结果，无法确认凭据"}

        if release.all_released:
            amount = float(sum(current_totals.values()))
            credential = DataCredential(
                credential_code=self._next_code("CRED"),
                request_id=request_id,
                batch_code=batch_code,
                window_type=WINDOW_CURRENT,
                data_date=current_date,
                amount=amount,
                totals=dict(current_totals),
                release_code=release.release_code,
                basis="本期全部分区放行，按本期检查覆盖数据合计",
            )
            self._credentials[request_id] = credential
            release.credential_code = credential.credential_code
            return {"ok": True, "credential": credential.to_dict(),
                    "partition_details": release.readable_partition_details()}

        # 有待核分区：记录等待原因，回退上个已确认窗口。
        reasons = [
            {
                "partition": partition,
                "partition_version": result.partition_version,
                "reason": "阻断规则未补齐：" + "、".join(
                    f"{f.rule_code}({f.message})" for f in result.blockers
                ),
                "batch_code": batch_code,
                "recorded_at": now_iso(),
            }
            for partition, result in release.partitions.items()
            if result.decision == DECISION_PENDING
        ]
        self._wait_reasons[request_id] = reasons

        window = self.latest_confirmed_window()
        if window is None:
            return {
                "ok": False,
                "message": "本期凭据未发出，且没有可用的上个已确认窗口合计",
                "wait_reasons": reasons,
            }
        credential = DataCredential(
            credential_code=self._next_code("CRED"),
            request_id=request_id,
            batch_code=batch_code,
            window_type=WINDOW_PREVIOUS,
            data_date=window.window_date,
            amount=window.total_amount,
            totals=dict(window.totals),
            release_code=release.release_code,
            basis="批次含待核分区，本期凭据暂缓，沿用上一个已确认窗口合计",
        )
        self._credentials[request_id] = credential
        release.credential_code = credential.credential_code
        return {
            "ok": True,
            "credential": credential.to_dict(),
            "wait_reasons": reasons,
            "partition_details": release.readable_partition_details(),
        }

    def get_credential(self, request_id: str) -> DataCredential | None:
        return self._credentials.get(request_id)

    def get_wait_reason(self, request_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._wait_reasons.get(request_id, [])]

    def place_order(self, request_id: str) -> dict[str, Any]:
        """凭据确认后按凭据的日期和金额下单。"""
        credential = self._credentials.get(request_id)
        if credential is None:
            return {"ok": False, "message": f"补货请求 {request_id} 尚无已确认取数凭据，不能下单"}
        if request_id in self._orders:
            order = self._orders[request_id]
            return {"ok": False, "message": "该请求已按凭据下单，不能重复下单",
                    "order": order.to_dict()}
        order = ReplenishmentOrder(
            request_id=request_id,
            credential_code=credential.credential_code,
            order_date=credential.data_date,
            amount=credential.amount,
        )
        self._orders[request_id] = order
        return {"ok": True, "order": order.to_dict()}

    # ---- 签出核对 ----

    def register_checkout(self, request_id: str, partition: str,
                          partition_version: str, dates: list[str]) -> dict[str, Any]:
        """登记系统实际签出的本期数据日期。"""
        entry = {
            "request_id": request_id,
            "partition": partition,
            "partition_version": partition_version,
            "dates": list(dates),
            "checked_at": now_iso(),
        }
        self._checkouts.setdefault(request_id, []).append(entry)
        return {"ok": True, "checkout": entry}

    def audit_current_checkouts(self, batch_code: str) -> dict[str, Any]:
        """核对实际签出了哪些日期的本期数据。

        待核分区即使被签出，也取消其本期新数据资格，不得冒充本期数据；
        放行分区的签出保留本期资格。
        """
        release = self._releases.get(batch_code)
        if release is None:
            return {"ok": False, "message": f"批次 {batch_code} 无放行结果可核对"}
        audited: list[dict[str, Any]] = []
        for request_id, entries in self._checkouts.items():
            for entry in entries:
                partition = entry["partition"]
                result = release.partitions.get(partition)
                decision = result.decision if result else "未覆盖"
                qualified = decision == DECISION_RELEASED
                audited.append({
                    "request_id": request_id,
                    "partition": partition,
                    "partition_version": entry["partition_version"],
                    "dates": list(entry["dates"]),
                    "decision": decision,
                    "current_qualified": qualified,
                    "note": "待核分区签出数据不得冒充本期新数据" if not qualified else "本期有效",
                })
        return {
            "ok": True,
            "batch_code": batch_code,
            "checked_out_dates": sorted({d for item in audited for d in item["dates"]}),
            "items": audited,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        return {
            "module": "partition_gate",
            "entity": "分区放行",
            "release_batches": len(self._releases),
            "published_partitions": len(self._published),
            "credentials": len(self._credentials),
            "orders": len(self._orders),
            "waiting_requests": len(self._wait_reasons),
        }
