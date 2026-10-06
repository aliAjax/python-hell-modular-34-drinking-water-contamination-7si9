import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError
from src import network


def linear_network(*valve_states, zone_pops=None, origin="n0", sources=None):
    """n0 -V1- n1 -V2- n2 -V3- n3，区域 Z-A..Z-D 挂在 n0..n3。

    备用水源 n4 经无阀门管段接到 n3：V1 关闭后 n1..n3 仍由 n4 供水，
    这样关阀可以让区域真正“恢复供水且脱离污染”。
    """
    states = dict(zip(("V1", "V2", "V3"), valve_states or ("open", "open", "open")))
    pops = zone_pops or {"Z-A": 100, "Z-B": 200, "Z-C": 300, "Z-D": 400}
    return {
        "nodes": ["n0", "n1", "n2", "n3", "n4"],
        "edges": [
            {"a": "n0", "b": "n1", "valve": "V1"},
            {"a": "n1", "b": "n2", "valve": "V2"},
            {"a": "n2", "b": "n3", "valve": "V3"},
            {"a": "n3", "b": "n4"},
        ],
        "zones": [
            {"zone_id": "Z-A", "node": "n0"},
            {"zone_id": "Z-B", "node": "n1"},
            {"zone_id": "Z-C", "node": "n2"},
            {"zone_id": "Z-D", "node": "n3"},
        ],
        "source_nodes": sources or ["n0", "n4"],
        "origin_node": origin,
        "zone_populations": pops,
        "valves": [{"valve_id": vid, "state": state} for vid, state in states.items()],
    }


def create_payload(net):
    return {
        "source_id": "SRC-N",
        "contaminant": "nitrate",
        "detected_at": "2026-10-06T06:00:00+00:00",
        "concentration": 20,
        "limit": 10,
        "network": net,
    }


class ComputeScopeRuleTest(unittest.TestCase):
    def test_contamination_spreads_through_open_valves_and_closed_valve_stops_it(self):
        net = network.normalize_network(linear_network("open", "closed", "open"))
        scope = network.compute_scope(net, {"V1": "open", "V2": "closed", "V3": "open"})
        # 污染从 n0 扩散：n0、n1 被污染；V2 关闭挡住 n2、n3（n3 另有备用源 n4 供水）
        self.assertEqual(scope["contaminated"], ["Z-A", "Z-B"])
        self.assertEqual(scope["shutoff"], [])
        self.assertEqual(scope["affected"], ["Z-A", "Z-B"])
        self.assertEqual(scope["population"], 300)

    def test_shutoff_when_isolated_from_all_sources(self):
        # 只有一个水源 n0 时，关 V1 会让 n1..n3 停水
        net = network.normalize_network(linear_network("closed", "open", "open", sources=["n0"]))
        scope = network.compute_scope(net, {"V1": "closed", "V2": "open", "V3": "open"})
        self.assertEqual(scope["contaminated"], ["Z-A"])
        self.assertEqual(scope["shutoff"], ["Z-B", "Z-C", "Z-D"])
        self.assertEqual(scope["population"], 1000)

    def test_all_open_reaches_every_zone(self):
        net = network.normalize_network(linear_network())
        scope = network.compute_scope(net, {"V1": "open", "V2": "open", "V3": "open"})
        self.assertEqual(scope["contaminated"], ["Z-A", "Z-B", "Z-C", "Z-D"])
        self.assertEqual(scope["shutoff"], [])

    def test_latest_timestamp_wins_comparison(self):
        self.assertTrue(network.is_later("2026-10-06T09:00:00+00:00", "2026-10-06T08:00:00+00:00"))
        self.assertFalse(network.is_later("2026-10-06T08:00:00+00:00", "2026-10-06T09:00:00+00:00"))
        self.assertFalse(network.is_later("2026-10-06T09:00:00+00:00", "2026-10-06T09:00:00+00:00"))


class ScopeWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _item(self, net=None):
        return self.service.create_item(create_payload(net or linear_network()), "analyst-1", "analyst")

    def test_create_computes_scope_and_population_from_network(self):
        item = self._item()
        self.assertEqual(item["payload"]["zone_ids"], ["Z-A", "Z-B", "Z-C", "Z-D"])
        self.assertEqual(item["payload"]["population"], 1000)
        self.assertEqual(item["payload"]["scope_epoch"], 1)

    def test_valve_closure_shrinks_scope_and_reopen_issues_notices_for_readded(self):
        item = self._item()
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "a", "analyst", item["version"])
        for zone_id in item["payload"]["zone_ids"]:
            item = self.service.act(
                item["id"], "advise",
                {"notice_id": "N-%s" % zone_id, "kind": "boil", "message": "煮沸", "zone_id": zone_id},
                "d", "dispatcher", item["version"],
            )

        # 班组上报 V1 关闭：n1..n3 由备用源 n4 供水，污染被挡在 n0
        item = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V1", "state": "closed", "observed_at": "2026-10-06T08:00:00+00:00"},
            "f1", "field_operator",
        )
        item = self.service.act(
            item["id"], "recompute_scope", {"request_id": "REC-1"},
            "coord", "coordinator", item["version"],
        )
        payload = item["payload"]
        self.assertEqual(payload["scope_epoch"], 2)
        self.assertEqual(payload["zone_ids"], ["Z-A"])
        self.assertEqual(payload["effective_zone_ids"], ["Z-A"])
        self.assertEqual(payload["population"], 100)
        # 移出且未开工区域：通知撤回、处置撤回登记
        for zone_id in ("Z-B", "Z-C", "Z-D"):
            self.assertTrue(all(n.get("withdrawn") for n in payload["notifications"] if n.get("zone_id") == zone_id))
        withdrawn_zone_ids = {w["zone_id"] for w in payload["withdrawals"]}
        self.assertEqual(withdrawn_zone_ids, {"Z-B", "Z-C", "Z-D"})
        self.assertIn("撤回", payload["withdrawals"][0]["reason"])

        # 重新打开 V1：污染重新扩散，Z-B/Z-C/Z-D 新纳入，补发范围通知
        item = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V1", "state": "open", "observed_at": "2026-10-06T10:00:00+00:00"},
            "f2", "field_operator",
        )
        item = self.service.act(
            item["id"], "recompute_scope", {"request_id": "REC-2"},
            "coord", "coordinator", item["version"],
        )
        payload = item["payload"]
        self.assertEqual(payload["scope_epoch"], 3)
        self.assertEqual(payload["zone_ids"], ["Z-A", "Z-B", "Z-C", "Z-D"])
        self.assertEqual(payload["population"], 1000)
        auto = [n for n in payload["notifications"] if n.get("auto")]
        self.assertEqual({n["zone_id"] for n in auto}, {"Z-B", "Z-C", "Z-D"})
        self.assertTrue(all(n["kind"] == "scope_extension" for n in auto))
        self.assertTrue(all(n["scope_epoch"] == 3 for n in auto))

    def test_started_zone_removed_from_scope_is_retained_in_effective_scope(self):
        item = self._item()
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "a", "analyst", item["version"])
        item = self.service.act(
            item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"},
            "d", "dispatcher", item["version"],
        )
        # Z-B 已开工冲洗
        item = self.service.act(
            item["id"], "flush", {"zone_id": "Z-B"}, "f1", "field_operator", item["version"]
        )
        item = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V1", "state": "closed", "observed_at": "2026-10-06T08:00:00+00:00"},
            "f1", "field_operator",
        )
        item = self.service.act(
            item["id"], "recompute_scope", {}, "coord", "coordinator", item["version"]
        )
        payload = item["payload"]
        # 连通算出只剩 Z-A，但 Z-B 已开工不能靠重算甩掉
        self.assertEqual(payload["zone_ids"], ["Z-A"])
        self.assertEqual(payload["effective_zone_ids"], ["Z-A", "Z-B"])
        self.assertEqual(payload["scope_basis"]["retained_started"], ["Z-B"])
        self.assertEqual(payload["population"], 300)  # 100 + 200
        # 已开工区域不撤回
        self.assertFalse(any(w["zone_id"] == "Z-B" for w in payload.get("withdrawals", [])))
        # 再做一次无变化重算：不递增纪元
        version = item["version"]
        again = self.service.act(
            item["id"], "recompute_scope", {}, "coord", "coordinator", item["version"]
        )
        self.assertEqual(again["version"], version)
        self.assertEqual(again["payload"]["scope_epoch"], 2)

    def test_stale_valve_report_does_not_override_newer_state(self):
        item = self._item()
        item = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V2", "state": "closed", "observed_at": "2026-10-06T09:00:00+00:00"},
            "team-a", "field_operator",
        )
        # 另一班组晚提交，但上报时刻更旧 -> 拒绝，不覆盖
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                item["id"], "report_valve",
                {"valve_id": "V2", "state": "open", "observed_at": "2026-10-06T08:30:00+00:00"},
                "team-b", "field_operator",
            )
        self.assertEqual(ctx.exception.code, "stale_valve_report")
        fresh = self.service.get_item(item["id"])
        self.assertEqual(fresh["payload"]["valve_states"]["V2"]["state"], "closed")
        # 同一时刻再报也不允许覆盖
        with self.assertRaises(DomainError):
            self.service.act(
                item["id"], "report_valve",
                {"valve_id": "V2", "state": "open", "observed_at": "2026-10-06T09:00:00+00:00"},
                "team-b", "field_operator",
            )
        # 更新时刻的上报生效
        fresh = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V2", "state": "open", "observed_at": "2026-10-06T09:30:00+00:00"},
            "team-b", "field_operator",
        )
        self.assertEqual(fresh["payload"]["valve_states"]["V2"]["state"], "open")

    def test_concurrent_valve_reports_latest_observation_wins(self):
        item = self._item()
        item_id = item["id"]
        errors = []

        # 新时刻(11:00)先落库；随后两个线程在同一屏障后并发提交更旧/相同时刻，
        # 无论事务以何种顺序拿到写锁，旧状态都不能覆盖新状态。
        self.service.act(
            item_id, "report_valve",
            {"valve_id": "V1", "state": "open", "observed_at": "2026-10-06T11:00:00+00:00"},
            "team-b", "field_operator",
        )
        barrier = threading.Barrier(3)

        def report(state, observed_at, team):
            try:
                barrier.wait(timeout=10)
                self.service.act(
                    item_id, "report_valve",
                    {"valve_id": "V1", "state": state, "observed_at": observed_at},
                    team, "field_operator",
                )
            except DomainError as exc:
                errors.append((team, exc.code))

        threads = [
            threading.Thread(target=report, args=("closed", "2026-10-06T10:00:00+00:00", "team-a")),
            threading.Thread(target=report, args=("closed", "2026-10-06T11:00:00+00:00", "team-c")),
        ]
        for t in threads:
            t.start()
        barrier.wait(timeout=10)
        for t in threads:
            t.join()

        final = self.service.get_item(item_id)["payload"]["valve_states"]["V1"]
        self.assertEqual(final["state"], "open")
        self.assertEqual(final["observed_at"], "2026-10-06T11:00:00+00:00")
        self.assertEqual(final["reported_by"], "team-b")
        self.assertEqual(sorted(code for _, code in errors), ["stale_valve_report", "stale_valve_report"])

    def test_recompute_failure_leaves_state_unchanged_and_same_request_retries(self):
        # 备用源在更下游（n4 接 n3，主源 n0）。初始全连通四区都受污染 -> 建单需要全部人口，
        # 因此用关闭 V1 的阀门初态建单：初始只有 Z-A 受污染，Z-B/C/D 有备用源供水且不污染，
        # 缺人口不暴露；打开 V1 后重算，Z-B/C/D 进入受影响范围，缺人口导致重算失败。
        net = linear_network(
            "closed", "open", "open",
            zone_pops={"Z-A": 100},
        )
        item = self.service.create_item(create_payload(net), "a", "analyst")
        self.assertEqual(item["payload"]["zone_ids"], ["Z-A"])
        version_before = item["version"]

        item = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V1", "state": "open", "observed_at": "2026-10-06T08:00:00+00:00"},
            "f1", "field_operator",
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                item["id"], "recompute_scope", {"request_id": "REC-X"},
                "coord", "coordinator", item["version"],
            )
        self.assertEqual(ctx.exception.code, "zone_population_missing")
        # 失败不落库：只多出阀门上报这一次版本，纪元不变、无历史、无通知
        after_fail = self.service.get_item(item["id"])
        self.assertEqual(after_fail["version"], version_before + 1)
        self.assertEqual(after_fail["payload"]["scope_epoch"], 1)
        self.assertNotIn("scope_history", after_fail["payload"])
        self.assertEqual(after_fail["payload"]["zone_ids"], ["Z-A"])

        # 补齐人口后用同一个原请求重试 -> 成功
        fixed = linear_network(
            "closed", "open", "open",
            zone_pops={"Z-A": 100, "Z-B": 200, "Z-C": 300, "Z-D": 400},
        )
        item = self.service.act(
            item["id"], "update_network", {"network": fixed},
            "coord", "coordinator", after_fail["version"],
        )
        item = self.service.act(
            item["id"], "recompute_scope", {"request_id": "REC-X"},
            "coord", "coordinator", item["version"],
        )
        self.assertEqual(item["payload"]["population"], 1000)
        self.assertEqual(item["payload"]["zone_ids"], ["Z-A", "Z-B", "Z-C", "Z-D"])
        self.assertEqual(item["payload"]["scope_history"][0]["request_id"], "REC-X")
        auto_notices = item["payload"]["scope_history"][0]["auto_notice_ids"]
        self.assertEqual(set(auto_notices), {"AUTO-SCOPE-E2-Z-B", "AUTO-SCOPE-E2-Z-C", "AUTO-SCOPE-E2-Z-D"})

        # 同一 request_id 再次提交：幂等，不产生新版本/新通知
        version = item["version"]
        notice_count = len(item["payload"]["notifications"])
        again = self.service.act(
            item["id"], "recompute_scope", {"request_id": "REC-X"},
            "coord", "coordinator", item["version"],
        )
        self.assertEqual(again["version"], version)
        self.assertEqual(len(again["payload"]["notifications"]), notice_count)

    def test_scope_change_voids_restoration_and_returns_to_reinspection_with_reason(self):
        # 污染在 n0，备用源 n4。全流程恢复后关闭 V1：Z-B/C/D 脱离污染且有备用源 -> 移出
        item = self._item()
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "a", "analyst", item["version"])
        item = self.service.act(
            item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"},
            "d", "dispatcher", item["version"],
        )
        item = self.service.act(
            item["id"], "switch_source", {"alternate_source_id": "ALT-1"},
            "coord", "coordinator", item["version"],
        )
        item = self.service.act(
            item["id"], "flush", {"zone_id": "Z-A"}, "f1", "field_operator", item["version"]
        )
        item = self.service.act(
            item["id"], "disinfect", {"zone_id": "Z-A", "completed": True},
            "f1", "field_operator", item["version"],
        )
        item = self.service.act(
            item["id"], "sample", {"sample_id": "S-1", "zone_id": "Z-A", "concentration": 2},
            "lab", "lab", item["version"],
        )
        # Z-B/C/D 各自完成消毒取样
        for zone_id in ("Z-B", "Z-C", "Z-D"):
            item = self.service.act(
                item["id"], "flush", {"zone_id": zone_id}, "f1", "field_operator", item["version"]
            )
            item = self.service.act(
                item["id"], "disinfect", {"zone_id": zone_id, "completed": True},
                "f1", "field_operator", item["version"],
            )
            item = self.service.act(
                item["id"], "sample",
                {"sample_id": "S-%s" % zone_id, "zone_id": zone_id, "concentration": 2},
                "lab", "lab", item["version"],
            )
        item = self.service.act(
            item["id"], "restore", {"all_zones_cleared": True, "note": "全部合格"},
            "coord", "coordinator", item["version"],
        )
        self.assertEqual(item["status"], "restored")

        item = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V1", "state": "closed", "observed_at": "2026-10-06T12:00:00+00:00"},
            "f1", "field_operator",
        )
        item = self.service.act(
            item["id"], "recompute_scope", {"request_id": "REC-R"},
            "coord", "coordinator", item["version"],
        )
        # 范围一变，恢复结论作废，退回待复检并写明原因
        self.assertEqual(item["status"], "sampled")
        self.assertIsNone(item["payload"]["restoration"])
        self.assertTrue(item["payload"]["reinspection"]["required"])
        self.assertIn("原恢复结论作废", item["payload"]["reinspection"]["reason"])
        self.assertEqual(item["payload"]["reinspection"]["since_epoch"], 2)
        self.assertEqual(item["payload"]["restoration_history"][-1]["voided_at_epoch"], 2)
        self.assertTrue(item["payload"]["scope_history"][-1]["restoration_voided"])

        # 旧纪元样本不能再支撑恢复结论
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                item["id"], "restore", {"all_zones_cleared": True},
                "coord", "coordinator", item["version"],
            )
        self.assertEqual(ctx.exception.code, "zones_need_resampling")

        # 当前生效范围（Z-B/C/D 已开工被保留）全部重新取样合格后才能恢复
        for zone_id in item["payload"]["effective_zone_ids"]:
            item = self.service.act(
                item["id"], "sample",
                {"sample_id": "S2-%s" % zone_id, "zone_id": zone_id, "concentration": 1},
                "lab", "lab", item["version"],
            )
        item = self.service.act(
            item["id"], "restore", {"all_zones_cleared": True},
            "coord", "coordinator", item["version"],
        )
        self.assertEqual(item["status"], "restored")
        self.assertEqual(item["payload"]["restoration"]["scope_epoch"], 2)


    def test_initial_shutoff_zones_count_as_affected(self):
        # 单水源，V1/V2/V3 初态全关：Z-B/C/D 与水源失联而停水，建单即纳入受影响范围
        net = linear_network("closed", "closed", "closed", sources=["n0"])
        item = self.service.create_item(create_payload(net), "a", "analyst")
        self.assertEqual(item["payload"]["scope_basis"]["contaminated"], ["Z-A"])
        self.assertEqual(item["payload"]["scope_basis"]["shutoff"], ["Z-B", "Z-C", "Z-D"])
        self.assertEqual(item["payload"]["zone_ids"], ["Z-A", "Z-B", "Z-C", "Z-D"])
        self.assertEqual(item["payload"]["population"], 1000)

    def test_update_network_keeps_reported_valve_and_drops_removed_one(self):
        item = self._item()
        item = self.service.act(
            item["id"], "report_valve",
            {"valve_id": "V2", "state": "closed", "observed_at": "2026-10-06T09:00:00+00:00"},
            "f1", "field_operator",
        )
        # 更新管网：V2 所在管段改为无阀直通；V1/V3 保留，已上报的 V2 状态清除
        updated = linear_network()
        updated["edges"][1] = {"a": "n1", "b": "n2"}
        updated["valves"] = [v for v in updated["valves"] if v["valve_id"] != "V2"]
        item = self.service.act(
            item["id"], "update_network", {"network": updated},
            "coord", "coordinator", item["version"],
        )
        states = item["payload"]["valve_states"]
        self.assertNotIn("V2", states)
        self.assertIn("V1", states)
        self.assertIn("V3", states)


if __name__ == "__main__":
    unittest.main()
