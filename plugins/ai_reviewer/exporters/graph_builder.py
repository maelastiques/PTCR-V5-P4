from typing import Any, Dict

from ..models.graph import ProjectGraph


def build_project_graph(project: Dict[str, Any], pcb: Dict[str, Any], schematic: Dict[str, Any]) -> Dict[str, Any]:
    graph = ProjectGraph()

    graph.add_node("project", "project", path=project.get("project_path", ""))

    for fp in pcb.get("footprints", []):
        ref = fp.get("ref", "")
        if not ref:
            continue
        fp_id = f"fp:{ref}"
        graph.add_node(fp_id, "footprint", **fp)
        graph.add_edge("project", fp_id, "contains")

    for net in pcb.get("nets", []):
        net_name = net.get("name", "")
        if not net_name:
            continue
        net_id = f"net:{net_name}"
        graph.add_node(net_id, "net", **net)
        graph.add_edge("project", net_id, "contains")

    for pad in pcb.get("pads", []):
        pad_id = f"pad:{pad.get('id', '')}"
        if pad_id == "pad:":
            continue
        graph.add_node(pad_id, "pad", **pad)

        footprint_ref = pad.get("footprint_ref", "")
        if footprint_ref:
            graph.add_edge(f"fp:{footprint_ref}", pad_id, "has_pad")

        net_name = pad.get("net", "")
        if net_name:
            graph.add_edge(pad_id, f"net:{net_name}", "connected_to")

    for index, track in enumerate(pcb.get("tracks", [])):
        track_id = f"track:{index}"
        graph.add_node(track_id, "track", **track)
        net_name = track.get("net", "")
        if net_name:
            graph.add_edge(track_id, f"net:{net_name}", "routes")

    for index, via in enumerate(pcb.get("vias", [])):
        via_id = f"via:{index}"
        graph.add_node(via_id, "via", **via)
        net_name = via.get("net", "")
        if net_name:
            graph.add_edge(via_id, f"net:{net_name}", "connects_layer")

    for index, zone in enumerate(pcb.get("zones", [])):
        zone_id = f"zone:{index}"
        graph.add_node(zone_id, "zone", **zone)
        net_name = zone.get("net", "")
        if net_name:
            graph.add_edge(zone_id, f"net:{net_name}", "pours")

    for sch_file in schematic.get("files", []):
        sch_id = f"sch:{sch_file.get('path', '')}"
        graph.add_node(sch_id, "schematic_sheet", **sch_file)
        graph.add_edge("project", sch_id, "contains")

    return graph.to_dict()
