from .domain import DomainError, now_iso
from .network import diff_scope

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
    "recalculate_scope": {"analyst", "dispatcher", "coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"advise", "switch_source", "flush", "disinfect", "sample", "restore", "cancel"}


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


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _mark_work_order_done(current, zone_id, wo_type):
    """Mark the planned disposal of a zone as started/done, or record it if missing."""
    work_orders = current.setdefault("work_orders", [])
    for wo in work_orders:
        if wo.get("zone_id") == zone_id and wo.get("type") == wo_type and wo.get("status") == "planned":
            wo["status"] = "done"
            wo["done_at"] = now_iso()
            return
    work_orders.append({
        "zone_id": zone_id,
        "type": wo_type,
        "status": "done",
        "planned_at": now_iso(),
        "done_at": now_iso(),
        "withdrawn_at": None,
        "withdraw_reason": None,
    })


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
        _need_status(item, {"advisory", "flushing", "switched"})
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "flush", "zone_id": zone_id})
        _mark_work_order_done(current, zone_id, "flush")
        return "flushing", current, {"zone_id": zone_id, "type": "flush"}

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "disinfect", "zone_id": zone_id})
        _mark_work_order_done(current, zone_id, "disinfect")
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
        current.setdefault("sample_results", []).append(result)
        return "sampled", current, {"sample_result": result}

    if action == "restore":
        _need_status(item, {"sampled"})
        if not payload.get("all_zones_cleared"):
            raise DomainError("zones_not_cleared", "仍有区域未完成水质恢复", 409)
        limit = float(current.get("limit", 0))
        results = current.get("sample_results", [])
        if not results or any(float(result["concentration"]) > limit for result in results):
            raise DomainError("quality_not_met", "复检结果未全部达到限值", 409)
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    if action == "recalculate_scope":
        new_zones = payload.get("zones")
        if not isinstance(new_zones, list) or not new_zones:
            raise DomainError("zones_required", "重算需要受影响区域", 400)
        new_zones = sorted({str(zone).strip() for zone in new_zones if str(zone).strip()})
        if not new_zones:
            raise DomainError("zones_required", "重算需要受影响区域", 400)
        new_population = payload.get("population")
        reason = payload.get("reason")
        reason = reason.strip() if isinstance(reason, str) and reason.strip() else "管网连通关系变化"
        old_zones = current.get("zone_ids", [])
        added, removed = diff_scope(old_zones, new_zones)
        scope_changed = bool(added or removed)

        work_orders = current.setdefault("work_orders", [])
        notifications = current.setdefault("notifications", [])

        # 新纳入区域补发通知，并计划冲洗处置
        for zone in added:
            notice_id = "AUTO-%s" % zone
            if not any(notice.get("notice_id") == notice_id for notice in notifications):
                notifications.append({
                    "notice_id": notice_id,
                    "kind": "auto",
                    "message": "区域 %s 因管网连通变化纳入停水范围，请补发通知" % zone,
                    "zone_id": zone,
                    "auto": True,
                })
            work_orders.append({
                "zone_id": zone,
                "type": "flush",
                "status": "planned",
                "planned_at": now_iso(),
                "done_at": None,
                "withdrawn_at": None,
                "withdraw_reason": None,
            })

        # 移出且未开工区域撤回处置（已开工的处置保留）
        for zone in removed:
            for wo in work_orders:
                if wo.get("zone_id") == zone and wo.get("status") == "planned":
                    wo["status"] = "withdrawn"
                    wo["withdrawn_at"] = now_iso()
                    wo["withdraw_reason"] = reason

        current["zone_ids"] = new_zones
        if new_population is not None:
            current["population"] = int(new_population)

        # 范围一变，原恢复结论就作废；已恢复区域退回待复检并写明原因
        new_status = item["status"]
        if scope_changed and item["status"] == "restored":
            invalidations = current.setdefault("restoration_invalidations", [])
            invalidations.append({"reason": reason, "at": now_iso(), "from_status": item["status"]})
            new_status = "sampled"
            current["restoration"] = None

        return new_status, current, {
            "added": added,
            "removed": removed,
            "scope_changed": scope_changed,
            "reason": reason,
            "zone_ids": new_zones,
        }

    raise DomainError("unknown_action", "不支持的操作")
