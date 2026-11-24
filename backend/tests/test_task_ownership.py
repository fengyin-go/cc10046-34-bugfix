"""任务归属凭据链测试：任务号、来源、批次、补采和幂等。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services import task_ownership as ownership
from app.services.batch_tracker import BatchTrackerService
from app.services.crawl_schedule import CrawlScheduleService
from app.services.task_ownership import TaskIdentityError
from app.store import Store, store


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    """每个用例用独立仓库，避免内存数据相互污染。"""
    fresh = Store()
    # 所有服务都拿同一个 store 单例，直接替换它的表最稳妥。
    from app.store import store as singleton
    monkeypatch.setattr(singleton, "_tables", fresh._tables)
    yield store


def _normal_schedule(site: str):
    service = CrawlScheduleService()
    plan = service.domain_create_schedule(f"cfg-{site}", source_site=site)
    batch = BatchTrackerService().domain_track_batch(
        f"NORMAL-{site}", plan["task_id"], source_site=site)
    return plan, batch


class TestTaskIdentity:
    def test_each_schedule_gets_its_own_task_id(self, fresh_store):
        first, _ = _normal_schedule("甲站")
        second, _ = _normal_schedule("乙站")
        assert first["task_id"] != second["task_id"]
        ids = [row["任务标识"] for row in fresh_store.rows("crawl_schedule")]
        assert len(ids) == len(set(ids))

    def test_task_id_is_allowed_to_look_shared_task_up(self):
        result = CrawlScheduleService().domain_create_schedule("cfg-甲", source_site="甲站")
        other = CrawlScheduleService().domain_create_schedule("cfg-乙", source_site="乙站")
        reused = ownership.domain_lookup_task(ownership.LEGACY_TASK_ID)
        assert reused["shared"] is True
        assert result["task_id"] != other["task_id"]

    def test_new_schedule_never_allocates_legacy_shared_id(self):
        plan = CrawlScheduleService().domain_create_schedule("cfg-甲", source_site="甲站")
        assert plan["task_id"] != ownership.LEGACY_TASK_ID
        assert ownership.find_schedule(ownership.LEGACY_TASK_ID) is None

    def test_reusing_a_task_id_is_rejected(self):
        plan, _ = _normal_schedule("甲站")
        with pytest.raises(TaskIdentityError):
            CrawlScheduleService().domain_create_schedule(
                "cfg-甲", task_id=plan["task_id"])

    def test_batch_source_must_match_task_source(self):
        plan, _ = _normal_schedule("甲站")
        with pytest.raises(TaskIdentityError):
            BatchTrackerService().domain_track_batch(
                "NORMAL-乙", plan["task_id"], source_site="乙站")

    def test_batch_tracker_resolves_source_from_credential(self):
        _, batch = _normal_schedule("甲站")
        assert batch["来源站点"] == "甲站"
        service = BatchTrackerService()
        assert service.batch_source({**batch, "来源站点": "乙站"}) == "甲站"
        listed, _ = service.list_entries(size=100)
        stored = next(row for row in listed if row["批次标识"] == "NORMAL-甲站")
        assert stored["来源站点"] == "甲站"

    def test_duplicate_batch_names_stay_with_their_own_schedule(self, fresh_store):
        first, _ = _normal_schedule("甲站")
        second, _ = _normal_schedule("乙站")
        service = BatchTrackerService()
        service.domain_track_batch("同名批次", first["task_id"], source_site="甲站")
        service.domain_track_batch("同名批次", second["task_id"], source_site="乙站")
        listed, _ = service.list_entries(size=100)
        rows = [row for row in listed if row["批次标识"] == "同名批次"]
        by_schedule = {row["调度任务"]: row["来源站点"] for row in rows}
        assert by_schedule[first["task_id"]] == "甲站"
        assert by_schedule[second["task_id"]] == "乙站"


class TestLegacySharedTask:
    def test_lookup_keeps_both_evidence_records(self):
        result = ownership.domain_lookup_task(ownership.LEGACY_TASK_ID)
        assert result["shared"] is True
        assert result["split_state"] == "拆分中"
        assert result["pending_sources"] == ["乙站"]
        by_site = {item["来源站点"]: item for item in result["sources"]}
        assert by_site["甲站"]["凭据状态"] == "已核定"
        assert by_site["乙站"]["凭据状态"] == "待补"

    def test_claiming_verified_source_dispatches_only_its_batches(self):
        claim = ownership.domain_claim_backfill(
            ownership.LEGACY_TASK_ID, "甲站", request_id="REQ-A-1")
        assert claim["dispatch"] is True
        assert claim["batches"] == ["LGC-A-001"]
        assert claim["execution_task_id"]
        assert "乙站待补" in claim["message"]
        assert ownership.domain_lookup_task(
            ownership.LEGACY_TASK_ID)["split_state"] == "拆分中"

    def test_repeat_claim_with_same_request_does_not_duplicate_execution(self):
        first = ownership.domain_claim_backfill(
            ownership.LEGACY_TASK_ID, "甲站", request_id="REQ-A-1")
        replay = ownership.domain_claim_backfill(
            ownership.LEGACY_TASK_ID, "甲站", request_id="REQ-A-1")
        assert replay["replayed"] is True
        assert replay["execution_task_id"] == first["execution_task_id"]
        assert len(ownership._execution_rows()) == 1

    def test_pending_source_is_blocked_until_evidence_arrives(self):
        blocked = ownership.domain_claim_backfill(
            ownership.LEGACY_TASK_ID, "乙站", request_id="REQ-B-1")
        assert blocked["dispatch"] is False
        assert blocked["state"] == ownership.BLOCKED
        assert blocked["batches"] == []
        assert len(ownership._execution_rows()) == 0

        ownership.domain_supplement_evidence(
            ownership.LEGACY_TASK_ID, "乙站",
            evidence="乙站补报来源证明", batch_ids=["LGC-B-001"])
        claimed = ownership.domain_claim_backfill(
            ownership.LEGACY_TASK_ID, "乙站", request_id="REQ-B-2",
            origin_batch="LGC-B-001")
        assert claimed["dispatch"] is True
        assert claimed["batches"] == ["LGC-B-001"]
        lookup = ownership.domain_lookup_task(ownership.LEGACY_TASK_ID)
        assert lookup["split_state"] == "已完成"
        assert lookup["pending_sources"] == []
        assert lookup["inconsistencies"] == []

    def test_backfill_reads_only_batches_owned_by_the_schedule(self, fresh_store):
        ownership.ensure_legacy_seed()
        # 同名批次挂到别的调度上，不应被旧调度补采读出来。
        other = CrawlScheduleService().domain_create_schedule(
            "cfg-甲", source_site="甲站")
        BatchTrackerService().domain_track_batch(
            "LGC-A-001", other["task_id"], source_site="甲站")
        # 同名来源但批次属于别的任务，也不应串进来。
        other_g = CrawlScheduleService().domain_create_schedule(
            "cfg-g", source_site="甲站")
        BatchTrackerService().domain_track_batch(
            "LGC-G-999", other_g["task_id"], source_site="甲站")

        legacy_batches = ownership.domain_batches_for_schedule(
            ownership.LEGACY_TASK_ID, "甲站")
        assert [item["批次标识"] for item in legacy_batches] == ["LGC-A-001"]
        assert all(
            item["来源站点"] == "甲站" for item in legacy_batches)

        other_batches = ownership.domain_batches_for_schedule(
            other["task_id"], "甲站")
        assert [item["批次标识"] for item in other_batches] == ["LGC-A-001"]
        assert all(
            item["任务归属凭据"].startswith(f"BATCH-{other['task_id']}")
            for item in other_batches)

    def test_retry_batch_keeps_same_source_ownership(self):
        ownership.domain_claim_backfill(
            ownership.LEGACY_TASK_ID, "甲站", request_id="REQ-A-1",
            origin_batch="LGC-A-001")
        lookup = ownership.domain_lookup_task(ownership.LEGACY_TASK_ID, "甲站")
        retries = [item for item in lookup["batches"] if item["重试自"]]
        assert retries, "重采应留下仍挂同一归属凭据的历史批次"
        assert all(item["来源站点"] == "甲站" for item in retries)

    def test_retry_action_uses_owned_batch_source(self):
        ownership.ensure_legacy_seed()
        service = BatchTrackerService()
        listed, _ = service.list_entries(size=100)
        row = next(item for item in listed if item["批次标识"] == "LGC-A-001")
        # 即便行上来源字段被写错，重采也要按凭据回到甲站。
        row["来源站点"] = "乙站"
        _, message = service.run_action(row["id"], "重采失败行")
        assert "甲站" in message
        executions = ownership._execution_rows()
        assert executions
        assert all(item["来源站点"] == "甲站" for item in executions)


class TestConsistencyErrors:
    def test_new_schedule_reports_mismatched_source(self):
        plan = CrawlScheduleService().domain_create_schedule(
            "cfg-甲", task_id="SCHED-NEW-1", source_site="甲站")
        ownership.domain_record_evidence(
            "SCHED-NEW-1", "乙站", evidence="乙站证据", status=ownership.VERIFIED)
        with pytest.raises(TaskIdentityError) as error:
            ownership.domain_track_batch(
                "B-X", plan["task_id"], source_site="乙站")
        assert "不一致" in str(error.value) or "对不上" in str(error.value)

    def test_lookup_surfaces_inconsistency(self):
        plan = CrawlScheduleService().domain_create_schedule(
            "cfg-甲", task_id="SCHED-NEW-2", source_site="甲站")
        # 越过服务直接造一条来源与任务号对不上的凭据，回查时必须显式报出来。
        ownership._append_source_credential(
            plan["task_id"], "乙站", status=ownership.VERIFIED,
            evidence="错挂的来源证据", operator="测试")
        lookup = ownership.domain_lookup_task(plan["task_id"])
        assert lookup["inconsistencies"]
        assert any("乙站" in item for item in lookup["inconsistencies"])

    def test_unknown_task_claim_reports_explicit_error(self):
        with pytest.raises(TaskIdentityError):
            ownership.domain_claim_backfill("SCHED-NOPE", "甲站")
