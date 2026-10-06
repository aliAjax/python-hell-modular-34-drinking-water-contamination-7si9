from . import domain, rules
from .domain import DomainError, require_text, parse_timestamp
from .network import reachable_zones


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    def setup_network(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in {"coordinator", "analyst", "dispatcher"}:
            raise DomainError("forbidden", "当前角色不能设置管网", 403)
        zones = payload.get("zones", [])
        pipes = payload.get("pipes", [])
        if not isinstance(zones, list) or not isinstance(pipes, list):
            raise DomainError("invalid_network", "zones 和 pipes 必须是列表", 400)
        clean_zones = []
        for zone in zones:
            if not isinstance(zone, dict) or not str(zone.get("zone_id", "")).strip():
                raise DomainError("invalid_network", "区域编号不能为空", 400)
            clean_zones.append({
                "zone_id": str(zone["zone_id"]).strip(),
                "population": int(zone.get("population", 0) or 0),
            })
        clean_pipes = []
        for pipe in pipes:
            if not isinstance(pipe, dict):
                raise DomainError("invalid_network", "管段必须是对象", 400)
            from_zone = str(pipe.get("from_zone", "")).strip()
            to_zone = str(pipe.get("to_zone", "")).strip()
            valve_id = str(pipe.get("valve_id", "")).strip()
            if not from_zone or not to_zone or not valve_id:
                raise DomainError("invalid_network", "管段缺少区域或阀门编号", 400)
            clean_pipes.append({"from_zone": from_zone, "to_zone": to_zone, "valve_id": valve_id})
        self.repository.setup_network(clean_zones, clean_pipes)
        return {"zones": len(clean_zones), "pipes": len(clean_pipes)}

    def report_valve(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in {"field_operator", "dispatcher", "coordinator"}:
            raise DomainError("forbidden", "当前角色不能上报阀门状态", 403)
        valve_id = require_text(payload, "valve_id")
        state = require_text(payload, "state")
        if state not in {"open", "closed"}:
            raise DomainError("invalid_valve_state", "阀门状态必须是 open 或 closed", 400)
        reported_at = parse_timestamp(payload, "reported_at")
        return self.repository.report_valve(valve_id, state, reported_at, actor)

    def recalculate_scope(self, item_id, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        allowed = rules.ACTION_ROLES.get("recalculate_scope", set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能重算受影响范围", 403)
        item = self.repository.get_item(item_id)
        pipes = self.repository.list_pipes()
        valves = self.repository.list_valves()
        valve_states = {valve["valve_id"]: valve["state"] for valve in valves}
        source_zone = item["payload"].get("source_zone")
        if not source_zone:
            zone_ids = item["payload"].get("zone_ids") or []
            source_zone = zone_ids[0] if zone_ids else None
        new_zones = reachable_zones(source_zone, pipes, valve_states)
        zones_info = self.repository.list_zones()
        if zones_info:
            pop_map = {zone["zone_id"]: zone["population"] for zone in zones_info}
            new_population = sum(pop_map.get(zone, 0) for zone in new_zones)
        else:
            new_population = item["payload"].get("population", 0)
        reason = payload.get("reason", "")
        recalc_payload = {"zones": new_zones, "population": new_population, "reason": reason}
        new_status, new_payload, event_payload = rules.apply_action(
            item, "recalculate_scope", recalc_payload, actor, role
        )
        self.repository.apply_action(
            item_id, "recalculate_scope", actor, role, new_status, new_payload, event_payload
        )
        return self.get_item(item_id)
