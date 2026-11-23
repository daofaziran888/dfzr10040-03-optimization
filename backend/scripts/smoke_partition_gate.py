"""分区放行夜班场景的路由层冒烟脚本（手工运行，非 pytest 用例）。"""
from __future__ import annotations

from app.fusion_engine import fusion_engine as eng
from app.routers import partition_gate as rg
from app.services import service_for


def main() -> None:
    svc = service_for("partition_gate")
    items, total = svc.list_entries(page=1, size=20)
    print("seed rows:", total, "| sample:", items[0]["确认结果"], items[0]["关联凭据"])

    eng.register_source("SRC-E2E", "e2e", "MySQL", "conn://e2e")
    job = eng.create_fusion_job("WT_E2E", ["SRC-E2E"], "id")
    eng.create_quality_gate("WT_E2E", "pk_not_null", "id", "block", "block")
    eng.create_quality_gate("WT_E2E", "row_count_min", "3", "warn", "warning")

    yi = [{"id": "y1", "amount": 100.0}, {"id": "y2", "amount": 200.0}]
    jia_bad = [{"id": "", "amount": 300.0}]
    jia_ok = [{"id": "j1", "amount": 300.0}]

    r = rg.batch_check(rg.BatchCheckPayload(
        job_id=job.job_id, batch_code="B-E2E",
        partitions={"yi": yi}, partition_versions={"yi": "v1"}))
    yi_res = r["quality_gate"]["partition_results"]["yi"]
    print("1) yi alone:", yi_res["decision"], "warnings:", len(yi_res["warnings"]),
          "checked_rows:", yi_res["checked_rows"])

    r = rg.batch_check(rg.BatchCheckPayload(
        job_id=job.job_id, batch_code="B-E2E",
        partitions={"jia": jia_bad, "yi": yi},
        partition_versions={"jia": "v1", "yi": "v1"}))
    print("2) blocked:", r["quality_gate"]["blocked_partitions"],
          "released:", r["quality_gate"]["released_partitions"])

    print("3a) publish jia:", rg.publish_partition(
        rg.PublishPayload(batch_code="B-E2E", partition="jia"))["ok"])
    pub_yi = rg.publish_partition(rg.PublishPayload(batch_code="B-E2E", partition="yi"))
    print("3b) publish yi:", pub_yi["ok"], pub_yi["partition_version"])

    rg.confirm_window(rg.WindowPayload(
        window_date="2026-11-22", total_amount=5000.0,
        totals={"jia": 2000.0, "yi": 3000.0}))
    cred = rg.issue_credential(rg.CredentialPayload(
        request_id="REQ-1", batch_code="B-E2E",
        current_totals={"jia": 300.0, "yi": 300.0}, current_date="2026-11-23"))
    print("4a) credential:", cred["credential"]["window_type"],
          cred["credential"]["data_date"], cred["credential"]["amount"],
          "code:", cred["credential"]["credential_code"])
    wait = rg.get_wait_reason("REQ-1")
    print("4b) wait reasons:", len(wait["wait_reasons"]),
          "->", wait["wait_reasons"][0]["partition"])
    dup = rg.issue_credential(rg.CredentialPayload(
        request_id="REQ-1", batch_code="B-E2E",
        current_totals={}, current_date="2026-11-23"))
    print("4c) duplicate accepted?:", dup["ok"])
    order = rg.place_order(rg.OrderPayload(request_id="REQ-1"))
    print("4d) order:", order["order"]["order_date"], order["order"]["amount"])
    print("4e) re-order accepted?:",
          rg.place_order(rg.OrderPayload(request_id="REQ-1"))["ok"])
    print("4f) no-cred order accepted?:",
          rg.place_order(rg.OrderPayload(request_id="NOPE"))["ok"])

    rg.register_checkout(rg.CheckoutPayload(
        request_id="REQ-1", partition="jia", partition_version="v1",
        dates=["2026-11-23"]))
    rg.register_checkout(rg.CheckoutPayload(
        request_id="REQ-1", partition="yi", partition_version="v1",
        dates=["2026-11-23"]))
    audit = rg.audit_checkouts("B-E2E")
    for item in audit["items"]:
        print("5) checkout:", item["partition"],
              "qualified:", item["current_qualified"], "|", item["note"])

    rg.batch_check(rg.BatchCheckPayload(
        job_id=job.job_id, batch_code="B-E2E",
        partitions={"jia": jia_ok, "yi": yi},
        partition_versions={"jia": "v2", "yi": "v1"}))
    cred2 = rg.issue_credential(rg.CredentialPayload(
        request_id="REQ-2", batch_code="B-E2E",
        current_totals={"jia": 300.0, "yi": 300.0}, current_date="2026-11-23"))
    print("6) new credential:", cred2["credential"]["window_type"],
          cred2["credential"]["data_date"], cred2["credential"]["amount"])
    order2 = rg.place_order(rg.OrderPayload(request_id="REQ-2"))
    print("   order:", order2["order"]["order_date"], order2["order"]["amount"])

    rel = rg.get_release("B-E2E")
    for d in rel["partition_details"]:
        print("7) detail:", d["partition"], d["partition_version"], d["decision"],
              "cred:", d["credential_code"],
              "blocking:", d["blocking_rules"], "warn:", d["warning_rules"])

    items, total = svc.list_entries(page=1, size=20)
    print("8) resource list total:", total)
    for row in items:
        print("   ", row["分区编号"], row["分区版本"],
              row["确认结果"], row["关联凭据"])


if __name__ == "__main__":
    main()
