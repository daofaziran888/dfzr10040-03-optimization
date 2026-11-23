"""分区放行专用接口：分区放行结果、跨日补货凭据、签出核对。

通用资源路由负责列表/登记，本路由承载只能由领域状态机处理的动作，
路由层不做业务判断，全部委托给引擎内的 PartitionGateManager。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.fusion_engine import fusion_engine

router = APIRouter(prefix="/api/partition-gate", tags=["分区放行"])


class BatchCheckPayload(BaseModel):
    job_id: int
    batch_code: str
    partitions: dict[str, list[dict[str, Any]]]
    partition_versions: dict[str, str] = Field(default_factory=dict)


class CredentialPayload(BaseModel):
    request_id: str
    batch_code: str
    current_totals: dict[str, float] = Field(default_factory=dict)
    current_date: str


class OrderPayload(BaseModel):
    request_id: str


class CheckoutPayload(BaseModel):
    request_id: str
    partition: str
    partition_version: str
    dates: list[str]


class WindowPayload(BaseModel):
    window_date: str
    total_amount: float
    totals: dict[str, float] = Field(default_factory=dict)


class PublishPayload(BaseModel):
    batch_code: str
    partition: str


@router.post("/batch-check")
def batch_check(payload: BatchCheckPayload) -> dict[str, Any]:
    """按分区跑质量门禁并登记分区放行结果。"""
    return fusion_engine.run_partitioned_quality_gate(
        job_id=payload.job_id,
        partitions=payload.partitions,
        batch_code=payload.batch_code,
        partition_versions=payload.partition_versions,
    )


@router.get("/releases")
def list_releases() -> dict[str, Any]:
    """跨日快照的可读分区明细，逐分区注明确认结果与关联凭据。"""
    manager = fusion_engine.partition_gate
    return {
        "items": [release.to_dict() for release in manager.list_releases()],
        "total": len(manager.list_releases()),
    }


@router.get("/releases/{batch_code}")
def get_release(batch_code: str) -> dict[str, Any]:
    release = fusion_engine.partition_gate.get_release(batch_code)
    if release is None:
        return {"ok": False, "message": f"批次 {batch_code} 无分区放行结果"}
    return {"ok": True, "release": release.to_dict(),
            "partition_details": release.readable_partition_details()}


@router.post("/publish")
def publish_partition(payload: PublishPayload) -> dict[str, Any]:
    """放行分区发布；待核分区拒绝用本次数据替换已发布内容。"""
    return fusion_engine.partition_gate.publish_partition(
        payload.batch_code, payload.partition,
    )


@router.post("/windows/confirm")
def confirm_window(payload: WindowPayload) -> dict[str, Any]:
    """登记上个已确认窗口合计，供待核时兜底。"""
    window = fusion_engine.partition_gate.confirm_window(
        payload.window_date, payload.total_amount, payload.totals,
    )
    return {"ok": True, "window": {
        "window_date": window.window_date,
        "total_amount": window.total_amount,
        "totals": window.totals,
    }}


@router.post("/credentials")
def issue_credential(payload: CredentialPayload) -> dict[str, Any]:
    """确认跨日补货取数凭据；同一请求只接受一次确认。"""
    return fusion_engine.partition_gate.issue_credential(
        request_id=payload.request_id,
        batch_code=payload.batch_code,
        current_totals=payload.current_totals,
        current_date=payload.current_date,
    )


@router.get("/credentials/{request_id}")
def get_credential(request_id: str) -> dict[str, Any]:
    credential = fusion_engine.partition_gate.get_credential(request_id)
    if credential is None:
        return {"ok": False, "message": f"请求 {request_id} 尚无凭据"}
    return {"ok": True, "credential": credential.to_dict()}


@router.get("/wait-reasons/{request_id}")
def get_wait_reason(request_id: str) -> dict[str, Any]:
    """未发本期凭据时回查等待原因。"""
    return {"ok": True, "request_id": request_id,
            "wait_reasons": fusion_engine.partition_gate.get_wait_reason(request_id)}


@router.post("/orders")
def place_order(payload: OrderPayload) -> dict[str, Any]:
    """按凭据的日期和金额下单，凭据缺失或重复下单都会被拒绝。"""
    return fusion_engine.partition_gate.place_order(payload.request_id)


@router.post("/checkouts")
def register_checkout(payload: CheckoutPayload) -> dict[str, Any]:
    """登记系统实际签出的本期数据日期。"""
    return fusion_engine.partition_gate.register_checkout(
        request_id=payload.request_id,
        partition=payload.partition,
        partition_version=payload.partition_version,
        dates=payload.dates,
    )


@router.get("/checkouts/{batch_code}")
def audit_checkouts(batch_code: str) -> dict[str, Any]:
    """核对实际签出了哪些日期的本期数据，待核分区取消本期资格。"""
    return fusion_engine.partition_gate.audit_current_checkouts(batch_code)
