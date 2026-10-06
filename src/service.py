from . import domain, rules, network
from .domain import DomainError


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
        network_raw = normalized.pop("network_raw", None)
        if network_raw is not None:
            net, valve_states, scope = network.build_network_state(network_raw)
            normalized["network"] = net
            normalized["valve_states"] = {
                valve_id: {"state": state, "observed_at": None, "reported_by": None}
                for valve_id, state in valve_states.items()
            }
            normalized["zone_ids"] = list(scope["affected"])
            normalized["effective_zone_ids"] = list(scope["affected"])
            normalized["population"] = scope["population"]
            normalized["scope_epoch"] = 1
            normalized["scope_basis"] = {"contaminated": scope["contaminated"], "shutoff": scope["shutoff"]}
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

        if action == "report_valve":
            # 先做输入/状态校验，真正的“最新时刻胜出”比较在单事务内完成，避免并发丢失更新
            rules.apply_action(item, action, payload, actor, role)
            valve_id = payload["valve_id"].strip()
            observed_at = payload["observed_at"].strip()
            self.repository.report_valve(item_id, valve_id, payload["state"], observed_at, actor, role)
            return self.get_item(item_id)

        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        result = rules.apply_action(item, action, payload, actor, role)
        if result is rules.NOOP:
            # 幂等命中或范围无变化：不产生新版本，直接返回当前记录
            return self.get_item(item_id)
        new_status, new_payload, event_payload = result
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
