from typing import Any, Dict


def build_review_prompt(context: Dict[str, Any]) -> str:
    project = context.get("project", {})
    pcb = context.get("pcb", {})
    drc = context.get("drc", {})
    erc = context.get("erc", {})

    return "\n".join(
        [
            "You are an expert electronic design reviewer.",
            "Analyze this KiCad project context and provide findings by severity.",
            f"Project path: {project.get('project_path', '')}",
            f"Board file: {project.get('board_file', '')}",
            f"Footprints: {len(pcb.get('footprints', []))}",
            f"Tracks: {len(pcb.get('tracks', []))}",
            f"Vias: {len(pcb.get('vias', []))}",
            f"Zones: {len(pcb.get('zones', []))}",
            f"DRC ok: {drc.get('ok', False)}",
            f"ERC ok: {erc.get('ok', False)}",
            "Focus on power integrity, routing quality, return paths, and manufacturability.",
        ]
    )
