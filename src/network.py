from datetime import datetime, timezone

from .domain import DomainError


def parse_timestamp(value):
    """解析 ISO 时间并归一化为带时区的 datetime，用于按上报时刻比较先后。"""
    if not isinstance(value, str):
        raise DomainError("invalid_timestamp", "时间必须是 ISO 格式字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "时间必须是 ISO 格式")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_later(candidate_ts, reference_ts):
    """candidate 的上报时刻是否严格晚于 reference（相等也不允许覆盖）。"""
    return parse_timestamp(candidate_ts) > parse_timestamp(reference_ts)


def _require_string_list(value, name, min_size=0):
    if not isinstance(value, list) or len(value) < min_size:
        raise DomainError("invalid_network", "%s 必须是至少 %d 项的列表" % (name, min_size))
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DomainError("invalid_network", "%s 中的编号必须是非空字符串" % name)
        result.append(item.strip())
    return result


def normalize_network(network, require_all_populations=True):
    """校验并归一化管网连通关系。

    结构：
    - nodes: 节点编号
    - edges: {a, b, valve?}，valve 为边上的阀门编号
    - zones: {zone_id, node}，区域接在某个节点上
    - source_nodes: 水源节点
    - origin_node: 污染/报警起点节点
    - valves: 可选的阀门初始状态 {valve_id, state=open|closed}
    - zone_populations: 区域常住人口
    """
    if not isinstance(network, dict):
        raise DomainError("invalid_network", "network 必须是对象")

    nodes = _require_string_list(network.get("nodes"), "nodes", 1)
    if len(set(nodes)) != len(nodes):
        raise DomainError("invalid_network", "节点编号不能重复")
    node_set = set(nodes)

    sources = _require_string_list(network.get("source_nodes", network.get("sources", [])), "source_nodes", 1)
    missing_sources = [node for node in sources if node not in node_set]
    if missing_sources:
        raise DomainError("invalid_network", "水源节点不存在: %s" % ", ".join(missing_sources))
    if len(set(sources)) != len(sources):
        raise DomainError("invalid_network", "水源节点不能重复")

    origin = network.get("origin_node")
    if not isinstance(origin, str) or not origin.strip():
        raise DomainError("invalid_network", "origin_node 不能为空")
    origin = origin.strip()
    if origin not in node_set:
        raise DomainError("invalid_network", "污染起点节点不存在: %s" % origin)

    raw_edges = network.get("edges", [])
    if not isinstance(raw_edges, list):
        raise DomainError("invalid_network", "edges 必须是列表")
    edges = []
    valve_ids_on_edges = set()
    for edge in raw_edges:
        if not isinstance(edge, dict):
            raise DomainError("invalid_network", "每条边必须是对象")
        a = edge.get("a")
        b = edge.get("b")
        if not isinstance(a, str) or not a.strip() or not isinstance(b, str) or not b.strip():
            raise DomainError("invalid_network", "边的 a、b 端点不能为空")
        a, b = a.strip(), b.strip()
        if a not in node_set or b not in node_set:
            raise DomainError("invalid_network", "边 %s-%s 引用了不存在的节点" % (a, b))
        if a == b:
            raise DomainError("invalid_network", "边不能连接节点自身: %s" % a)
        valve = edge.get("valve")
        if valve is not None:
            if not isinstance(valve, str) or not valve.strip():
                raise DomainError("invalid_network", "阀门编号必须是非空字符串")
            valve = valve.strip()
            valve_ids_on_edges.add(valve)
        edges.append({"a": a, "b": b, "valve": valve})

    raw_zones = network.get("zones", [])
    if not isinstance(raw_zones, list) or not raw_zones:
        raise DomainError("invalid_network", "zones 必须是非空列表")
    zones = []
    zone_ids = []
    for zone in raw_zones:
        if not isinstance(zone, dict):
            raise DomainError("invalid_network", "每个区域必须是对象")
        zone_id = zone.get("zone_id")
        node = zone.get("node")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise DomainError("invalid_network", "zone_id 不能为空")
        if not isinstance(node, str) or not node.strip():
            raise DomainError("invalid_network", "区域挂载节点不能为空")
        zone_id, node = zone_id.strip(), node.strip()
        if node not in node_set:
            raise DomainError("invalid_network", "区域 %s 挂载的节点不存在: %s" % (zone_id, node))
        if zone_id in zone_ids:
            raise DomainError("invalid_network", "区域编号不能重复: %s" % zone_id)
        zone_ids.append(zone_id)
        zones.append({"zone_id": zone_id, "node": node})

    raw_populations = network.get("zone_populations", {})
    if not isinstance(raw_populations, dict):
        raise DomainError("invalid_network", "zone_populations 必须是对象")
    populations = {}
    for zone_id, value in raw_populations.items():
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise DomainError("invalid_network", "人口数据的区域编号不能为空")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DomainError("invalid_network", "区域 %s 的人口必须是非负整数" % zone_id)
        populations[zone_id.strip()] = value
    missing_pops = [zone_id for zone_id in zone_ids if zone_id not in populations]
    if missing_pops and require_all_populations:
        raise DomainError("invalid_network", "缺少区域人口数据: %s" % ", ".join(missing_pops))

    raw_valves = network.get("valves", [])
    if raw_valves is None:
        raw_valves = []
    if not isinstance(raw_valves, list):
        raise DomainError("invalid_network", "valves 必须是列表")
    valves = []
    seen_valve_ids = set()
    for valve in raw_valves:
        if not isinstance(valve, dict):
            raise DomainError("invalid_network", "每个阀门必须是对象")
        valve_id = valve.get("valve_id")
        state = valve.get("state", "open")
        if not isinstance(valve_id, str) or not valve_id.strip():
            raise DomainError("invalid_network", "valve_id 不能为空")
        valve_id = valve_id.strip()
        if valve_id not in valve_ids_on_edges:
            raise DomainError("invalid_network", "阀门 %s 未出现在任何管段上" % valve_id)
        if state not in ("open", "closed"):
            raise DomainError("invalid_network", "阀门 %s 状态只能是 open/closed" % valve_id)
        if valve_id in seen_valve_ids:
            raise DomainError("invalid_network", "阀门初始状态不能重复: %s" % valve_id)
        seen_valve_ids.add(valve_id)
        valves.append({"valve_id": valve_id, "state": state})

    return {
        "nodes": nodes,
        "edges": edges,
        "zones": zones,
        "source_nodes": sources,
        "origin_node": origin,
        "zone_populations": populations,
        "valves": valves,
    }


def valve_ids(network):
    return {edge["valve"] for edge in network["edges"] if edge["valve"]}


def _adjacency(network):
    adj = {node: set() for node in network["nodes"]}
    for edge in network["edges"]:
        adj[edge["a"]].add((edge["b"], edge["valve"]))
        adj[edge["b"]].add((edge["a"], edge["valve"]))
    return adj


def _reachable(adj, start, valve_states):
    """从 start 出发，沿开启阀门的管段可达的节点集合。"""
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for neighbor, valve in adj[node]:
            if neighbor in seen:
                continue
            if valve and valve_states.get(valve, "open") == "closed":
                continue
            seen.add(neighbor)
            stack.append(neighbor)
    return seen


def compute_scope(network, valve_states):
    """按当前阀门状态计算受影响区域。

    - contaminated：从污染起点经开启阀门可达的区域（污染顺支路扩散）
    - shutoff：与所有水源之间被关闭阀门切断的区域（停水）
    - 受影响范围为两者并集；受影响人数按区域人口求和
    返回区域顺序与 network["zones"] 一致。
    """
    adj = _adjacency(network)
    contaminated_nodes = _reachable(adj, network["origin_node"], valve_states)

    nodes_with_water = set()
    for source in network["source_nodes"]:
        nodes_with_water |= _reachable(adj, source, valve_states)

    contaminated = []
    shutoff = []
    affected = []
    for zone in network["zones"]:
        zone_id = zone["zone_id"]
        is_contaminated = zone["node"] in contaminated_nodes
        is_shutoff = zone["node"] not in nodes_with_water
        if is_contaminated:
            contaminated.append(zone_id)
        if is_shutoff:
            shutoff.append(zone_id)
        if is_contaminated or is_shutoff:
            affected.append(zone_id)

    populations = network["zone_populations"]
    missing = [zone_id for zone_id in affected if zone_id not in populations]
    if missing:
        raise DomainError(
            "zone_population_missing",
            "受影响区域缺少人口数据，无法统计受影响人数: %s" % ", ".join(missing),
            409,
        )

    return {
        "affected": affected,
        "contaminated": contaminated,
        "shutoff": shutoff,
        "population": sum(populations[zone_id] for zone_id in affected),
        "populations": {zone_id: populations.get(zone_id) for zone_id in affected},
    }


def build_network_state(network_payload):
    """建单时校验管网并算出初始影响范围。

    初始受影响区域必须有人口；暂未受影响的区域可缺人口，
    一旦其在后续重算中被纳入，重算会失败，补齐 update_network 后可用原请求重试。
    """
    network = normalize_network(network_payload, require_all_populations=False)
    initial_valves = network.pop("valves")
    valve_states = {valve["valve_id"]: valve["state"] for valve in initial_valves}
    scope = compute_scope(network, valve_states)
    if not scope["affected"]:
        raise DomainError(
            "empty_scope",
            "初始阀门状态下没有任何受影响区域，请检查污染起点与阀门初态",
            400,
        )
    return network, valve_states, scope
