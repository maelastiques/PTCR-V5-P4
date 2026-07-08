from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class GraphNode:
    node_id: str
    kind: str
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    source: str
    target: str
    relation: str
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectGraph:
    nodes: List[GraphNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)

    def add_node(self, node_id: str, kind: str, **attrs: Any) -> None:
        self.nodes.append(GraphNode(node_id=node_id, kind=kind, attrs=attrs))

    def add_edge(self, source: str, target: str, relation: str, **attrs: Any) -> None:
        self.edges.append(GraphEdge(source=source, target=target, relation=relation, attrs=attrs))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [
                {"id": n.node_id, "kind": n.kind, "attrs": n.attrs}
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "relation": e.relation,
                    "attrs": e.attrs,
                }
                for e in self.edges
            ],
        }
