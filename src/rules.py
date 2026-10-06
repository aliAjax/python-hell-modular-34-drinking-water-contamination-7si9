from .domain import DomainError
from . import network

ENTITY_TYPE = "water_contamination"
INITIAL_STATUS = "detected"
CREATE_ROLES = {"analyst", "dispatcher"}
SOURCE_ROLES = {"analyst", "dispatcher", "field_operator", "lab"}
ACTION_ROLES = {
    "verify": {"analyst", "dispatcher"},
    "advise": {"coordinator", "dispatcher"},
    "switch_source": {"coordinator"},
    "flush": {"field_operator"},
    "disinfect": {"field_operator"},
    "sample": {"lab", "field_operator"},
    "restore": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
    "report_valve": {"field_operator", "dispatcher"},
    "recompute_scope": {"dispatcher", "coordinator"},
    "update_network": {"dispatcher", "coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {
    "advise",
    "switch_source",
    "flush",
    "disinfect",
    "sample",
    "restore",
    "cancel",
    "recompute_scope",
    "update_network",
}
NOOP = None


def assess(payload):
    concentration = float(payload.get("concentration", 0))
    limit = max(float(payload.get("limit", 0.000001)), 0.000001)
    ratio = concentration / limit
    population = int(payload.get("population", 0))
    score = min(100.0, ratio * 35.0 + min(population / 1000.0, 40.0))
    if score >= 80:
        level = "critical"
    elif score >= 50:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level, "ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _need_active(item):
    if item["status"] == "cancelled":
        raise DomainError("invalid_state", "事件已取消，不允许执行该操作")


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _has_network(payload):
    return isinstance(payload.get("network"), dict)


def _worked_zones(payload):
    """已经开工的区域：已登记冲洗或消毒处置（不含复检取样）。"""
    return {action["zone_id"] for action in payload.get("response_actions", []) if action.get("zone_id")}


def _passing_samples_by_zone(payload, limit, current_epoch):
    """当前范围内、当前纪元下已有合格复检样本的区域。"""
    passed = set()
    for result in payload.get("sample_results", []):
        try:
            value = float(result["concentration"])
        except (TypeError, ValueError, KeyError):
            continue
        if value > limit:
            continue
        epoch = result.get("scope_epoch")
        if epoch is not None and epoch != current_epoch:
            continue
        passed.add(result["zone_id"])
    return passed


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"detected", "verified"})
        sample_count = int(payload.get("sample_count", 0) or 0)
        if sample_count < 1:
            raise DomainError("sample_required", "需要至少一份复检样本", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"sample_count": sample_count, "note": payload.get("note", "")}
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "advise":
        _need_status(item, {"verified", "advisory"})
        notice_id = _text(payload, "notice_id")
        notice = {
            "notice_id": notice_id,
            "kind": _text(payload, "kind"),
            "message": _text(payload, "message"),
        }
        notices = current.setdefault("notifications", [])
        if any(existing.get("notice_id") == notice_id for existing in notices):
            raise DomainError("duplicate_notification", "同一通知编号不能重复发送", 409)
        notices.append(notice)
        return "advisory", current, {"notice": notice}

    if action == "switch_source":
        _need_status(item, {"verified", "advisory", "flushing", "disinfected", "sampled", "switched"})
        alternate = _text(payload, "alternate_source_id")
        current["alternate_source_id"] = alternate
        return "switched", current, {"alternate_source_id": alternate}

    if action == "flush":
        # 各区域并行处置：其他区域采样（sampled）后，本区域仍可开始冲洗
        _need_status(item, {"advisory", "flushing", "switched", "disinfected", "sampled"})
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "flush", "zone_id": zone_id})
        return "flushing", current, {"zone_id": zone_id, "type": "flush"}

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected", "sampled"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "disinfect", "zone_id": zone_id})
        return "disinfected", current, {"zone_id": zone_id, "type": "disinfect"}

    if action == "sample":
        _need_status(item, {"disinfected", "sampled"})
        result = {
            "sample_id": _text(payload, "sample_id"),
            "zone_id": _text(payload, "zone_id"),
            "concentration": float(payload.get("concentration", 0)),
        }
        if result["concentration"] < 0:
            raise DomainError("invalid_concentration", "浓度不能为负数")
        # 样本打上当前范围纪元，范围变化后旧纪元样本不再支撑恢复结论
        result["scope_epoch"] = int(current.get("scope_epoch", 1))
        current.setdefault("sample_results", []).append(result)
        return "sampled", current, {"sample_result": result}

    if action == "restore":
        _need_status(item, {"sampled", "restored"})
        if not payload.get("all_zones_cleared"):
            raise DomainError("zones_not_cleared", "仍有区域未完成水质恢复", 409)
        limit = float(current.get("limit", 0))
        results = current.get("sample_results", [])
        current_epoch = int(current.get("scope_epoch", 1))
        if not results:
            raise DomainError("quality_not_met", "复检结果未全部达到限值", 409)

        if _has_network(current):
            effective = current.get("effective_zone_ids", [])
            passed = _passing_samples_by_zone(current, limit, current_epoch)
            missing = [zone_id for zone_id in effective if zone_id not in passed]
            if missing:
                raise DomainError(
                    "zones_need_resampling",
                    "范围纪元 %s 下仍有区域缺少合格复检: %s" % (current_epoch, ", ".join(missing)),
                    409,
                )
        elif any(float(result["concentration"]) > limit for result in results):
            raise DomainError("quality_not_met", "复检结果未全部达到限值", 409)

        current["restoration"] = {
            "actor": actor,
            "note": payload.get("note", ""),
            "scope_epoch": current_epoch,
            "scope_zone_ids": list(current.get("effective_zone_ids", current.get("zone_ids", []))),
        }
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    if action == "report_valve":
        # 仅做输入与状态校验；权威的“最新时刻胜出”由 repository 单事务完成
        validate_valve_report(item, payload)
        return NOOP

    if action == "recompute_scope":
        _need_active(item)
        return _recompute_scope(item, current, payload, actor, reason="recompute")

    if action == "update_network":
        _need_active(item)
        return _update_network(item, current, payload)

    raise DomainError("unknown_action", "不支持的操作")


def validate_valve_report(item, payload):
    """阀门上报的输入与前置状态校验（不修改工单，LWW 比较是 repository 的职责）。"""
    _need_active(item)
    current = item["payload"]
    if not _has_network(current):
        raise DomainError("network_required", "该事件未建立管网连通关系，无法上报阀门状态", 409)
    valve_id = _text(payload, "valve_id")
    if payload.get("state") not in ("open", "closed"):
        raise DomainError("invalid_valve_state", "阀门状态只能是 open/closed")
    observed_at = _text(payload, "observed_at")
    network.parse_timestamp(observed_at)  # 校验 ISO 时间
    if valve_id not in network.valve_ids(current["network"]):
        raise DomainError("valve_not_found", "管网上不存在阀门: %s" % valve_id, 404)
    existing = current.get("valve_states", {}).get(valve_id) or {}
    existing_ts = existing.get("observed_at")
    # 预检：已有更新或相同时刻的状态时直接拒绝；并发竞争由 repository 事务兜底
    if existing_ts is not None and not network.is_later(observed_at, existing_ts):
        raise DomainError(
            "stale_valve_report",
            "已存在 %s 时刻的更新状态，旧时刻 %s 的上报不覆盖" % (existing_ts, observed_at),
            409,
        )


def _effective_zone_ids(payload, scope_affected):
    """实际生效范围 = 新算出范围 ∪ 已开工但被移出的区域（开工后不能靠重算甩掉）。"""
    worked = _worked_zones(payload)
    effective = list(scope_affected)
    retained = []
    for zone_id in worked:
        if zone_id not in effective:
            effective.append(zone_id)
            retained.append(zone_id)
    return effective, retained


def _recompute_scope(item, current, payload, actor, reason):
    if not _has_network(current):
        raise DomainError("network_required", "该事件未建立管网连通关系，无法重算范围", 409)

    request_id = payload.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise DomainError("invalid_request_id", "request_id 必须是非空字符串")
    request_id = request_id.strip() if request_id else None
    if request_id:
        for entry in current.get("scope_history", []):
            if entry.get("request_id") == request_id:
                # 同一原请求重试：之前已成功应用，幂等返回，不再补发/撤回任何东西
                return NOOP

    valve_states = {valve_id: state["state"] for valve_id, state in current.get("valve_states", {}).items()}
    # 失败在这里抛出（如受影响区域缺人口数据）：不写入任何状态，调用方可用原请求重试
    scope = network.compute_scope(current["network"], valve_states)

    # 连通关系算出的范围变化才是“范围一变”；开工保留只影响实际处置范围
    previous_computed = set(current.get("zone_ids", []))
    new_computed = set(scope["affected"])
    worked = _worked_zones(current)
    effective, retained = _effective_zone_ids(current, scope["affected"])
    added = [zone_id for zone_id in scope["affected"] if zone_id not in previous_computed]
    removed = [zone_id for zone_id in previous_computed if zone_id not in new_computed]
    removed_unworked = [zone_id for zone_id in removed if zone_id not in worked]

    if new_computed == previous_computed:
        # 连通算出的范围无变化：幂等返回，不产生新版本和通知
        return NOOP

    zone_populations = current["network"]["zone_populations"]
    missing = [zone_id for zone_id in effective if zone_id not in zone_populations]
    if missing:
        raise DomainError(
            "zone_population_missing",
            "生效范围内区域缺少人口数据，无法统计受影响人数: %s" % ", ".join(missing),
            409,
        )
    effective_population = sum(zone_populations[zone_id] for zone_id in effective)

    current_epoch = int(current.get("scope_epoch", 1)) + 1
    old_epoch = current_epoch - 1

    # 新纳入区域：补发范围通知
    notifications = current.setdefault("notifications", [])
    auto_notices = []
    for zone_id in added:
        notice_id = "AUTO-SCOPE-E%d-%s" % (current_epoch, zone_id)
        if any(existing.get("notice_id") == notice_id for existing in notifications):
            continue
        notice = {
            "notice_id": notice_id,
            "kind": "scope_extension",
            "message": "管网连通关系重算后，区域 %s 新纳入受影响范围（纪元 %s）" % (zone_id, current_epoch),
            "zone_id": zone_id,
            "scope_epoch": current_epoch,
            "auto": True,
        }
        notifications.append(notice)
        auto_notices.append(notice)

    withdrawals = []
    for zone_id in removed_unworked:
        # 移出且未开工：撤回该区域此前的范围通知，并登记处置撤回
        for notice in notifications:
            if notice.get("zone_id") == zone_id and not notice.get("withdrawn"):
                notice["withdrawn"] = True
                notice["withdrawn_at_epoch"] = current_epoch
                withdrawals.append({"zone_id": zone_id, "notice_id": notice["notice_id"]})
        current.setdefault("withdrawals", []).append(
            {
                "zone_id": zone_id,
                "scope_epoch": current_epoch,
                "reason": "重算后区域不再受影响且尚未开工，撤回处置",
                "actor": actor,
            }
        )

    # 范围一变，原恢复结论作废：已恢复退回待复检（sampled），写明原因
    restoration_voided = None
    new_status = item["status"]
    if item["status"] == "restored":
        restoration_voided = {
            "previous": dict(current.get("restoration", {})),
            "voided_at_epoch": current_epoch,
            "reason": "受影响范围由纪元 %s 变为 %s，原恢复结论作废，退回待复检" % (old_epoch, current_epoch),
            "actor": actor,
        }
        current.setdefault("restoration_history", []).append(restoration_voided)
        current["restoration"] = None
        current["reinspection"] = {
            "required": True,
            "reason": restoration_voided["reason"],
            "since_epoch": current_epoch,
        }
        new_status = "sampled"

    current["zone_ids"] = list(scope["affected"])
    current["effective_zone_ids"] = effective
    current["population"] = effective_population
    current["scope_epoch"] = current_epoch
    current["scope_basis"] = {
        "contaminated": scope["contaminated"],
        "shutoff": scope["shutoff"],
        "retained_started": retained,
    }
    history_entry = {
        "epoch": current_epoch,
        "request_id": request_id,
        "reason": reason,
        "actor": actor,
        "affected": scope["affected"],
        "effective": effective,
        "added": added,
        "removed_unworked": removed_unworked,
        "retained_started": retained,
        "population": effective_population,
        "auto_notice_ids": [notice["notice_id"] for notice in auto_notices],
        "restoration_voided": bool(restoration_voided),
    }
    current.setdefault("scope_history", []).append(history_entry)

    event = {
        "scope_epoch": current_epoch,
        "request_id": request_id,
        "affected": scope["affected"],
        "effective": effective,
        "population": effective_population,
        "added": added,
        "removed_unworked": removed_unworked,
        "retained_started": retained,
        "notices_issued": [notice["notice_id"] for notice in auto_notices],
        "notices_withdrawn": withdrawals,
        "restoration_voided": restoration_voided,
    }
    return new_status, current, event


def _update_network(item, current, payload):
    network_payload = payload.get("network")
    if network_payload is None:
        raise DomainError("field_required", "network 不能为空")
    # 更新时允许个别区域人口暂缺（这些区域一旦进入受影响范围，重算会失败并要求补齐后重试）
    updated = network.normalize_network(network_payload, require_all_populations=False)
    initial_valves = updated.pop("valves")
    current["network"] = updated
    # 已上报的阀门状态保留；更新后不再存在于管段上的阀门状态清除
    valid_valve_ids = network.valve_ids(updated)
    kept = {
        valve_id: state
        for valve_id, state in current.get("valve_states", {}).items()
        if valve_id in valid_valve_ids
    }
    for valve in initial_valves:
        kept.setdefault(valve["valve_id"], {"state": valve["state"], "observed_at": None, "reported_by": None})
    current["valve_states"] = kept
    event = {
        "nodes": len(updated["nodes"]),
        "edges": len(updated["edges"]),
        "zones": [zone["zone_id"] for zone in updated["zones"]],
        "kept_valves": sorted(valve_id for valve_id, state in kept.items() if state.get("observed_at")),
    }
    return item["status"], current, event
