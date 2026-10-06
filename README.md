# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/network.py`：管网拓扑、阀门状态和连通性受影响范围计算。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`POST /api/items/<id>/recalculate`、`POST /api/network`、`POST /api/valves/report` 和审计查询。

受影响范围按管网连通关系维护：阀门关断或污染顺支路扩散后重算范围，新纳入区域补发通知并计划处置，移出且未开工区域撤回处置；范围一变原恢复结论作废，已恢复区域退回待复检并写明原因。阀门状态按上报时刻 last-write-wins，晚到的旧时刻不覆盖新状态；重算幂等，失败后可按原请求重试。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
