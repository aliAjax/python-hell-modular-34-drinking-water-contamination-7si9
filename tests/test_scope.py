import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 5000,
        }, "analyst-1", "analyst")
        self.service.setup_network({
            "zones": [
                {"zone_id": "Z-1", "population": 1000},
                {"zone_id": "Z-2", "population": 2000},
                {"zone_id": "Z-3", "population": 1500},
                {"zone_id": "Z-4", "population": 500},
            ],
            "pipes": [
                {"from_zone": "Z-1", "to_zone": "Z-2", "valve_id": "V-1"},
                {"from_zone": "Z-2", "to_zone": "Z-3", "valve_id": "V-2"},
                {"from_zone": "Z-3", "to_zone": "Z-4", "valve_id": "V-3"},
            ],
        }, "coord-1", "coordinator")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _open_valves(self, *valves):
        for valve_id in valves:
            self.service.report_valve(
                {"valve_id": valve_id, "state": "open",
                 "reported_at": "2026-09-27T09:00:00+00:00"},
                "field-1", "field_operator",
            )

    def _close_valves(self, *valves):
        for valve_id in valves:
            self.service.report_valve(
                {"valve_id": valve_id, "state": "closed",
                 "reported_at": "2026-09-27T10:00:00+00:00"},
                "field-1", "field_operator",
            )

    def test_scope_expansion_supplementary_notification(self):
        self._open_valves("V-2")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "污染顺支路扩散"}, "disp-1", "dispatcher")
        self.assertEqual(item["payload"]["zone_ids"], ["Z-1", "Z-2", "Z-3", "Z-4"])
        notice_ids = [n["notice_id"] for n in item["payload"]["notifications"]]
        self.assertIn("AUTO-Z-3", notice_ids)
        self.assertIn("AUTO-Z-4", notice_ids)
        # 新纳入区域计划处置
        planned = {(w["zone_id"], w["status"]) for w in item["payload"]["work_orders"]}
        self.assertIn(("Z-3", "planned"), planned)
        self.assertIn(("Z-4", "planned"), planned)

    def test_scope_shrink_withdraws_unstarted_disposal(self):
        # 先扩散纳入 Z-3、Z-4
        self._open_valves("V-2")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "污染扩散"}, "disp-1", "dispatcher")
        # 再关断 V-3，Z-4 移出且未开工 -> 撤回处置
        self._close_valves("V-3")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "阀门关断"}, "disp-1", "dispatcher")
        self.assertEqual(item["payload"]["zone_ids"], ["Z-1", "Z-2", "Z-3"])
        statuses = {w["zone_id"]: w["status"] for w in item["payload"]["work_orders"]}
        self.assertEqual(statuses["Z-4"], "withdrawn")
        self.assertEqual(statuses["Z-3"], "planned")

    def test_started_disposal_not_withdrawn(self):
        # 扩散纳入 Z-3
        self._open_valves("V-2")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "污染扩散"}, "disp-1", "dispatcher")
        # 对 Z-3 开工冲洗
        item = self.service.act(
            item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(
            item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"},
            "disp-1", "dispatcher", item["version"])
        item = self.service.act(
            item["id"], "flush", {"zone_id": "Z-3"}, "field-1", "field_operator", item["version"])
        # 关断 V-2，Z-3 移出但已开工 -> 处置保留
        self._close_valves("V-2")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "阀门关断"}, "disp-1", "dispatcher")
        statuses = {w["zone_id"]: w["status"] for w in item["payload"]["work_orders"]}
        self.assertEqual(statuses["Z-3"], "done")

    def test_scope_change_invalidates_restoration(self):
        item = self.service.act(
            self.item["id"], "verify", {"sample_count": 2}, "analyst-1", "analyst", self.item["version"])
        item = self.service.act(
            item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"},
            "disp-1", "dispatcher", item["version"])
        item = self.service.act(
            item["id"], "switch_source", {"alternate_source_id": "ALT-1"},
            "coord-1", "coordinator", item["version"])
        item = self.service.act(
            item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
        item = self.service.act(
            item["id"], "disinfect", {"zone_id": "Z-1", "completed": True},
            "field-1", "field_operator", item["version"])
        item = self.service.act(
            item["id"], "sample", {"sample_id": "S-1", "zone_id": "Z-1", "concentration": 2},
            "lab-1", "lab", item["version"])
        item = self.service.act(
            item["id"], "restore", {"all_zones_cleared": True},
            "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        # 范围一变，原恢复结论作废，已恢复区域退回待复检并写明原因
        self._open_valves("V-2")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "污染顺支路扩散"}, "disp-1", "dispatcher")
        self.assertEqual(item["status"], "sampled")
        self.assertIsNone(item["payload"]["restoration"])
        self.assertEqual(len(item["payload"]["restoration_invalidations"]), 1)
        self.assertEqual(
            item["payload"]["restoration_invalidations"][0]["reason"], "污染顺支路扩散")

    def test_valve_lww_late_report_does_not_overwrite(self):
        self.service.report_valve(
            {"valve_id": "V-2", "state": "open", "reported_at": "2026-09-27T09:00:00+00:00"},
            "field-1", "field_operator")
        # 晚到的旧时刻上报不应覆盖新状态
        result = self.service.report_valve(
            {"valve_id": "V-2", "state": "closed", "reported_at": "2026-09-27T08:00:00+00:00"},
            "field-2", "field_operator")
        self.assertTrue(result["kept"])
        valves = {v["valve_id"]: v["state"] for v in self.repo.list_valves()}
        self.assertEqual(valves["V-2"], "open")

    def test_recalculate_idempotent_on_retry(self):
        self._open_valves("V-2")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "污染扩散"}, "disp-1", "dispatcher")
        notice_count = len(item["payload"]["notifications"])
        order_count = len(item["payload"]["work_orders"])
        # 按原请求重试：不重复通知、不重复处置
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "污染扩散"}, "disp-1", "dispatcher")
        self.assertEqual(len(item["payload"]["notifications"]), notice_count)
        self.assertEqual(len(item["payload"]["work_orders"]), order_count)

    def test_population_updates_with_scope(self):
        self.assertEqual(self.item["payload"]["population"], 5000)
        self._open_valves("V-2")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "污染扩散"}, "disp-1", "dispatcher")
        # Z-1(1000)+Z-2(2000)+Z-3(1500)+Z-4(500) = 5000
        self.assertEqual(item["payload"]["population"], 5000)
        self._close_valves("V-3")
        item = self.service.recalculate_scope(
            self.item["id"], {"reason": "阀门关断"}, "disp-1", "dispatcher")
        # Z-1(1000)+Z-2(2000)+Z-3(1500) = 4500
        self.assertEqual(item["payload"]["population"], 4500)


if __name__ == "__main__":
    unittest.main()
