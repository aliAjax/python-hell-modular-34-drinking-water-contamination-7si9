# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/network.py`：管网连通关系（节点、管段阀门、水源、污染起点、区域人口）与受影响范围计算。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检、恢复状态机、阀门上报与范围重算。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、阀门最新时刻胜出（LWW）和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。

## 建单时携带管网

`POST /api/items` 可在 `zone_ids` 之外提供 `network`，系统据此计算初始受影响区域与人数：

- `nodes`：管网节点；`edges`：管段 `{a, b, valve?}`，`valve` 为管段上的阀门编号。
- `zones`：`{zone_id, node}`，区域挂在节点上；`zone_populations` 为各区域常住人口。
- `source_nodes`：一个或多个水源节点；`origin_node`：污染/报警起点。
- `valves`：阀门初态 `{valve_id, state=open|closed}`。

受影响区域 = 从污染起点经开启阀门可达的区域（污染顺支路扩散）∪ 被关阀切断、与所有水源失联的区域（停水）；受影响人数为这些区域人口之和。

## 阀门上报（最新时刻胜出）

`POST /api/items/<id>/actions`，`action=report_valve`，载荷 `{valve_id, state, observed_at}`，角色 `field_operator/dispatcher`。同一阀门被两个班组先后或并发上报时，**以最新 `observed_at` 为准**：晚到的旧时刻（含相同时刻）返回 `stale_valve_report` 且不覆盖当前状态。比较在 SQLite 单事务（`BEGIN IMMEDIATE`）内完成，避免并发丢失更新。

## 范围重算与联动

`action=recompute_scope`（可带 `request_id`，角色 `dispatcher/coordinator`，需要 `expected_version`）按当前阀门状态重算，每次连通范围变化产生新的 **范围纪元 `scope_epoch`**：

- **新纳入区域**：自动补发 `kind=scope_extension` 范围通知（`AUTO-SCOPE-E<纪元>-<区域>`）。
- **移出且未开工区域**：撤回该区域此前的范围通知（标记 `withdrawn`），并在 `withdrawals` 登记处置撤回及原因；已开工（已冲洗/消毒）区域不会被重算甩掉，保留在 `effective_zone_ids` 并继续计入人数。
- **范围一变，原恢复结论作废**：已 `restored` 的工单退回 `sampled`（待复检），原结论存入 `restoration_history`，`reinspection.reason` 写明作废原因；旧纪元的复检样本不再支撑恢复，当前生效范围需重新取样合格。
- `zone_ids` 为连通关系算出的范围；`effective_zone_ids` 为实际处置范围（算出范围 ∪ 开工保留）；`scope_history` 记录每次变化。

`action=update_network` 可修正管网拓扑（保留仍存在阀门的已上报状态）。

## 失败重试

重算在任何写入前完成范围计算与人口校验：受影响区域缺人口等情况返回错误且不落库、纪元不变。修复数据（如 `update_network` 补齐人口）后，用**同一个 `request_id` 重试原请求**即可成功；同一 `request_id` 已成功应用后再次提交为幂等空操作，不产生新版本、不重复发通知。

内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
