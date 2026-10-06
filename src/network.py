"""Pipe network topology, valve states, and connectivity-based scope calculation.

The pipe network is an undirected graph: zones are nodes, pipes are edges, and
each pipe is gated by a valve. Pollution spreads from a source zone through
pipes whose valve is open; closed valves block the spread.
"""


def build_graph(pipes):
    """Build an undirected adjacency graph from pipe definitions.

    pipes: iterable of {"from_zone": str, "to_zone": str, "valve_id": str}
    returns: dict zone -> list of (neighbor_zone, valve_id)
    """
    graph = {}
    for pipe in pipes:
        a = pipe["from_zone"]
        b = pipe["to_zone"]
        valve = pipe["valve_id"]
        graph.setdefault(a, []).append((b, valve))
        graph.setdefault(b, []).append((a, valve))
    return graph


def reachable_zones(source_zone, pipes, valve_states):
    """Compute zones reachable from source_zone through open valves.

    valve_states: dict valve_id -> "open" | "closed". Valves without a known
    state default to "open" (flow through), matching the initial assumption
    that the reported scope is connected.
    returns: sorted list of reachable zone ids (always includes source_zone).
    """
    graph = build_graph(pipes)
    states = valve_states or {}
    visited = set()
    stack = [source_zone]
    while stack:
        zone = stack.pop()
        if zone in visited:
            continue
        visited.add(zone)
        for neighbor, valve_id in graph.get(zone, []):
            if neighbor in visited:
                continue
            if states.get(valve_id, "open") == "open":
                stack.append(neighbor)
    return sorted(visited)


def diff_scope(old_zones, new_zones):
    """Return (added, removed) zone id lists between two scopes."""
    old = set(old_zones or [])
    new = set(new_zones or [])
    return sorted(new - old), sorted(old - new)
