"""质量门禁分区放行与跨日补货取数凭据测试。

场景主线（同一融合批次 BATCH-NIGHT，分区甲=part_jia、乙=part_yi）：
1. 先用乙的两行单独跑：低于三行阈值时应放行并带一条告警；
2. 再让甲的一行主键为空与乙一起跑：甲触发阻断、乙仍只有告警，补齐前
   夜班发起一次跨日补货取数，拿不到本期凭据时回退上个已确认窗口合计；
3. 每个补货请求只接受一次确认凭据，随后按凭据日期和金额下单；
4. 可读分区明细注明确认结果，放行结果与凭据关联，待核甲不能冒充本期新数据；
5. 补齐前核对系统实际签出日期，待核分区签出取消本期资格；
6. 补齐后重读甲乙：甲只使用本次过检数据，乙仍为两行；
7. 同一分区版本重跑覆盖而不是累计告警。
"""
from __future__ import annotations

import pytest

from app.fusion_engine import FusionEngine
from app.partition_gate import (
    DECISION_PENDING,
    DECISION_RELEASED,
    is_block_level,
)

BATCH = "BATCH-NIGHT"
JIA = "part_jia"
YI = "part_yi"
TABLE = "WT_NIGHT"
PREV_DATE = "2026-11-22"
CUR_DATE = "2026-11-23"
PREV_TOTAL = 5000.0


def _yi_rows() -> list[dict]:
    return [
        {"id": "yi-1", "name": "乙-1", "amount": 100.0},
        {"id": "yi-2", "name": "乙-2", "amount": 200.0},
    ]


def _jia_bad_row() -> list[dict]:
    return [{"id": "", "name": "甲-空主键", "amount": 300.0}]


def _jia_fixed_rows() -> list[dict]:
    return [{"id": "jia-1", "name": "甲-补齐", "amount": 300.0}]


@pytest.fixture
def engine() -> FusionEngine:
    eng = FusionEngine()
    eng.register_source(
        source_code="SRC-NIGHT", source_name="night_source",
        source_type="MySQL", connection_string="conn://night",
    )
    job = eng.create_fusion_job(
        target_wide_table=TABLE,
        participant_sources=["SRC-NIGHT"],
        join_key="id",
    )
    # 阻断级：主键完整性。
    pk_gate = eng.create_quality_gate(
        target_wide_table=TABLE,
        check_type="pk_not_null",
        threshold_condition="id",
        trigger_action="block",
        block_level="block",
    )
    # 告警级：行数下限三行。
    row_gate = eng.create_quality_gate(
        target_wide_table=TABLE,
        check_type="row_count_min",
        threshold_condition="3",
        trigger_action="warn",
        block_level="warning",
    )
    eng.job_id = job.job_id
    eng.pk_gate = pk_gate
    eng.row_gate = row_gate
    return eng


class TestRuleLevelSemantics:
    """规则级别语义：告警不得被误作阻断。"""

    def test_level_normalization(self) -> None:
        assert is_block_level("block")
        assert is_block_level("阻断")
        assert not is_block_level("warning")
        assert not is_block_level("")
        assert not is_block_level("告警")

    def test_warning_does_not_block_whole_table(self, engine: FusionEngine) -> None:
        result = engine._apply_quality_gates(_yi_rows(), TABLE)
        assert result["passed"] is True
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["level"] == "告警"
        assert result["warnings"][0]["check_type"] == "row_count_min"
        assert result["blockers"] == []

    def test_blocking_rule_fails_whole_table(self, engine: FusionEngine) -> None:
        rows = _jia_bad_row() + _yi_rows()
        result = engine._apply_quality_gates(rows, TABLE)
        assert result["passed"] is False
        assert len(result["blockers"]) == 1
        assert result["blockers"][0]["check_type"] == "pk_not_null"
        # 共 3 行刚好达到行数阈值，行数规则不触发，阻断结论只由阻断规则决定。
        assert result["warnings"] == []

        # 再加一个低于阈值的批次，阻断与告警同时出现、互不混淆。
        mixed = engine._apply_quality_gates(_jia_bad_row(), TABLE)
        assert mixed["passed"] is False
        assert len(mixed["blockers"]) == 1
        assert len(mixed["warnings"]) == 1


class TestPartitionReleaseScenario:
    """分区放行主线场景。"""

    def test_yi_alone_two_rows_releases_with_warning(self, engine: FusionEngine) -> None:
        """第一步：乙的两行单独跑，低于三行阈值仍放行，附一条告警。"""
        result = engine.run_partitioned_quality_gate(
            engine.job_id,
            partitions={YI: _yi_rows()},
            batch_code=BATCH,
            partition_versions={YI: "v1"},
        )
        assert result["ok"] is True
        quality = result["quality_gate"]
        assert quality["passed"] is True
        assert quality["released_partitions"] == [YI]

        yi = quality["partition_results"][YI]
        assert yi["decision"] == DECISION_RELEASED
        assert yi["checked_rows"] == 2
        assert len(yi["warnings"]) == 1
        assert yi["warnings"][0]["rule_code"] == engine.row_gate.rule_code
        assert yi["warnings"][0]["partition_version"] == "v1"
        assert yi["blockers"] == []

        release = engine.partition_gate.get_release(BATCH)
        assert release is not None
        details = release.readable_partition_details()
        assert details[0]["partition"] == YI
        assert details[0]["confirmed"] is True
        assert details[0]["warning_rules"] == [engine.row_gate.rule_code]

    def test_jia_blocked_yi_warned_and_credential_falls_back(self, engine: FusionEngine) -> None:
        """第二步：甲空主键与乙一起跑；甲待核、乙放行，凭据回退上窗。"""
        # 先登记上个已确认窗口合计（夜班手头口径）。
        engine.partition_gate.confirm_window(
            PREV_DATE, PREV_TOTAL, totals={JIA: 2000.0, YI: 3000.0},
        )

        result = engine.run_partitioned_quality_gate(
            engine.job_id,
            partitions={JIA: _jia_bad_row(), YI: _yi_rows()},
            batch_code=BATCH,
            partition_versions={JIA: "v1", YI: "v1"},
        )
        quality = result["quality_gate"]
        assert quality["passed"] is False
        assert set(quality["blocked_partitions"]) == {JIA}
        assert set(quality["released_partitions"]) == {YI}

        jia = quality["partition_results"][JIA]
        yi = quality["partition_results"][YI]
        assert jia["decision"] == DECISION_PENDING
        assert len(jia["blockers"]) == 1
        assert jia["blockers"][0]["rule_code"] == engine.pk_gate.rule_code
        # 告警随实际检查覆盖的分区版本走。
        assert yi["decision"] == DECISION_RELEASED
        assert len(yi["warnings"]) == 1
        assert yi["warnings"][0]["partition_version"] == "v1"

        # 待核甲不能用本次数据替换已发布内容。
        publish_jia = engine.partition_gate.publish_partition(BATCH, JIA)
        assert publish_jia["ok"] is False
        publish_yi = engine.partition_gate.publish_partition(BATCH, YI)
        assert publish_yi["ok"] is True
        published = engine.partition_gate.get_published(TABLE, YI)
        assert published is not None
        assert published["partition_version"] == "v1"
        # 再次尝试发布甲仍被拒，且已发布的乙不受影响。
        assert engine.partition_gate.publish_partition(BATCH, JIA)["ok"] is False
        assert engine.partition_gate.get_published(TABLE, JIA) is None

        # 可读分区明细注明确认结果。
        release = engine.partition_gate.get_release(BATCH)
        details = {d["partition"]: d for d in release.readable_partition_details()}
        assert details[JIA]["confirmed"] is False
        assert details[JIA]["blocking_rules"] == [engine.pk_gate.rule_code]
        assert details[YI]["confirmed"] is True

        # 夜班在补齐甲前发起一次跨日补货取数。
        cred = engine.partition_gate.issue_credential(
            request_id="REQ-NIGHT-1",
            batch_code=BATCH,
            current_totals={JIA: 300.0, YI: 300.0},
            current_date=CUR_DATE,
        )
        assert cred["ok"] is True
        credential = cred["credential"]
        # 未发本期凭据：回退上个已确认窗口。
        assert credential["window_type"] == "previous_confirmed"
        assert credential["data_date"] == PREV_DATE
        assert credential["amount"] == PREV_TOTAL
        assert credential["release_code"] == release.release_code

        # 等待原因可回查，明确指向待核甲的阻断规则。
        reasons = engine.partition_gate.get_wait_reason("REQ-NIGHT-1")
        assert len(reasons) == 1
        assert reasons[0]["partition"] == JIA
        assert engine.pk_gate.rule_code in reasons[0]["reason"]

        # 分区放行结果与凭据关联。
        linked = engine.partition_gate.get_release(BATCH)
        assert linked.credential_code == credential["credential_code"]
        assert all(
            d["credential_code"] == credential["credential_code"]
            for d in linked.readable_partition_details()
        )

        # 同一请求只接受一次确认凭据。
        duplicate = engine.partition_gate.issue_credential(
            request_id="REQ-NIGHT-1",
            batch_code=BATCH,
            current_totals={JIA: 300.0, YI: 300.0},
            current_date=CUR_DATE,
        )
        assert duplicate["ok"] is False

        # 随后按凭据的日期和金额下单。
        order = engine.partition_gate.place_order("REQ-NIGHT-1")
        assert order["ok"] is True
        assert order["order"]["order_date"] == PREV_DATE
        assert order["order"]["amount"] == PREV_TOTAL
        assert order["order"]["credential_code"] == credential["credential_code"]
        # 不能重复下单。
        assert engine.partition_gate.place_order("REQ-NIGHT-1")["ok"] is False
        # 没有凭据的请求不能下单。
        assert engine.partition_gate.place_order("REQ-NO-CRED")["ok"] is False

    def test_checkout_audit_disqualifies_pending_jia(self, engine: FusionEngine) -> None:
        """第三步：补齐前核对实际签出日期，待核甲不得冒充本期新数据。"""
        engine.run_partitioned_quality_gate(
            engine.job_id,
            partitions={JIA: _jia_bad_row(), YI: _yi_rows()},
            batch_code=BATCH,
            partition_versions={JIA: "v1", YI: "v1"},
        )
        engine.partition_gate.register_checkout(
            "REQ-NIGHT-1", JIA, "v1", dates=[CUR_DATE],
        )
        engine.partition_gate.register_checkout(
            "REQ-NIGHT-1", YI, "v1", dates=[CUR_DATE],
        )
        audit = engine.partition_gate.audit_current_checkouts(BATCH)
        assert audit["ok"] is True
        assert audit["checked_out_dates"] == [CUR_DATE]
        by_partition = {item["partition"]: item for item in audit["items"]}
        assert by_partition[JIA]["current_qualified"] is False
        assert by_partition[JIA]["decision"] == DECISION_PENDING
        assert "不得冒充" in by_partition[JIA]["note"]
        assert by_partition[YI]["current_qualified"] is True

    def test_after_fix_jia_uses_passed_data_yi_keeps_two_rows(self, engine: FusionEngine) -> None:
        """第四步：补齐甲后重读甲乙，甲只过检一次、乙仍两行，再发本期凭据。"""
        engine.partition_gate.confirm_window(
            PREV_DATE, PREV_TOTAL, totals={JIA: 2000.0, YI: 3000.0},
        )
        # 阻断态先跑一次。
        engine.run_partitioned_quality_gate(
            engine.job_id,
            partitions={JIA: _jia_bad_row(), YI: _yi_rows()},
            batch_code=BATCH,
            partition_versions={JIA: "v1", YI: "v1"},
        )
        # 补齐甲（新版本 v2）后与乙一起重读。
        fixed = engine.run_partitioned_quality_gate(
            engine.job_id,
            partitions={JIA: _jia_fixed_rows(), YI: _yi_rows()},
            batch_code=BATCH,
            partition_versions={JIA: "v2", YI: "v1"},
        )
        quality = fixed["quality_gate"]
        assert quality["passed"] is True
        assert set(quality["released_partitions"]) == {JIA, YI}
        jia = quality["partition_results"][JIA]
        yi = quality["partition_results"][YI]
        # 甲只用了这次过检的数据。
        assert jia["partition_version"] == "v2"
        assert jia["checked_rows"] == 1
        assert jia["blockers"] == []
        # 乙仍为两行，仍带一条行数告警。
        assert yi["checked_rows"] == 2
        assert len(yi["warnings"]) == 1

        # 甲现在可以发布 v2。
        publish = engine.partition_gate.publish_partition(BATCH, JIA)
        assert publish["ok"] is True
        assert publish["partition_version"] == "v2"
        assert engine.partition_gate.get_published(TABLE, JIA)["partition_version"] == "v2"

        # 新的夜班请求拿到的是本期凭据，日期金额来自本期放行数据。
        current_totals = {JIA: 300.0, YI: 300.0}
        cred = engine.partition_gate.issue_credential(
            request_id="REQ-NIGHT-2",
            batch_code=BATCH,
            current_totals=current_totals,
            current_date=CUR_DATE,
        )
        assert cred["ok"] is True
        credential = cred["credential"]
        assert credential["window_type"] == "current"
        assert credential["data_date"] == CUR_DATE
        assert credential["amount"] == 600.0
        order = engine.partition_gate.place_order("REQ-NIGHT-2")
        assert order["ok"] is True
        assert order["order"]["order_date"] == CUR_DATE
        assert order["order"]["amount"] == 600.0

    def test_rerun_same_version_overrides_instead_of_accumulating(self, engine: FusionEngine) -> None:
        """同版本重跑：告警只保留一条，规则检查行数不累计。"""
        versions = {YI: "v1"}
        for _ in range(3):
            engine.run_partitioned_quality_gate(
                engine.job_id,
                partitions={YI: _yi_rows()},
                batch_code=BATCH,
                partition_versions=versions,
            )
        release = engine.partition_gate.get_release(BATCH)
        # 批次结果被覆盖而不是新增。
        assert len(engine.partition_gate.list_releases()) == 1
        yi = release.partitions[YI]
        assert len(yi.warnings) == 1
        assert yi.checked_rows == 2

        gate = engine.row_gate
        stored = gate.partition_results[(YI, "v1")]
        assert stored["checked_rows"] == 2
        # 规则侧同一分区版本也只有一条结果记录。
        keys = [k for k in gate.partition_results if k == (YI, "v1")]
        assert len(keys) == 1

    def test_finding_bound_to_partition_version(self, engine: FusionEngine) -> None:
        """告警及其触发规则跟着实际检查覆盖的分区版本走。"""
        engine.run_partitioned_quality_gate(
            engine.job_id,
            partitions={JIA: _jia_bad_row(), YI: _yi_rows()},
            batch_code=BATCH,
            partition_versions={JIA: "v1", YI: "v1"},
        )
        release = engine.partition_gate.get_release(BATCH)
        jia = release.partitions[JIA]
        yi = release.partitions[YI]
        assert jia.blockers[0].partition_version == "v1"
        assert jia.blockers[0].partition == JIA
        assert yi.warnings[0].partition_version == "v1"
        assert yi.warnings[0].checked_rows == 2

        # 快照可读明细带版本与确认结果。
        snapshots = engine.run_partitioned_quality_gate(
            engine.job_id,
            partitions={JIA: _jia_fixed_rows(), YI: _yi_rows()},
            batch_code=BATCH,
            partition_versions={JIA: "v2", YI: "v1"},
        )["snapshots"]
        snap_by_partition = {s["partition"]: s for s in snapshots}
        assert snap_by_partition[JIA]["partition_version"] == "v2"
        assert snap_by_partition[JIA]["release_decision"] == DECISION_RELEASED
        assert snap_by_partition[JIA]["status_mark"] == "可用"
        assert snap_by_partition[YI]["record_count"] == 2
