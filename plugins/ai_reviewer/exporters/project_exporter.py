import json
import os
from typing import Any, Dict, List

import pcbnew


def _detect_kicad_version() -> str:
    if hasattr(pcbnew, "GetBuildVersion"):
        try:
            return str(pcbnew.GetBuildVersion())
        except Exception:
            pass
    if hasattr(pcbnew, "Version"):
        try:
            return str(pcbnew.Version())
        except Exception:
            pass
    return "unknown"


def _find_project_file(board_file: str) -> str:
    board_dir = os.path.dirname(board_file)
    board_name = os.path.splitext(os.path.basename(board_file))[0]
    preferred = os.path.join(board_dir, f"{board_name}.kicad_pro")
    if os.path.exists(preferred):
        return preferred

    for entry in os.listdir(board_dir):
        if entry.endswith(".kicad_pro"):
            return os.path.join(board_dir, entry)
    return ""


def _read_project_variables(project_file: str) -> Dict[str, Any]:
    if not project_file or not os.path.exists(project_file):
        return {}
    try:
        with open(project_file, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            if "text_variables" in data and isinstance(data["text_variables"], dict):
                return data["text_variables"]
            if "meta" in data and isinstance(data["meta"], dict):
                variables = data["meta"].get("variables")
                if isinstance(variables, dict):
                    return variables
    except Exception:
        return {}
    return {}


def _find_schematic_files(project_dir: str) -> List[str]:
    sch_files: List[str] = []
    for root, _, files in os.walk(project_dir):
        rel_root = os.path.relpath(root, project_dir)
        if rel_root != "." and any(part.startswith(".") for part in rel_root.split(os.sep)):
            continue
        for name in files:
            if name.endswith(".kicad_sch"):
                sch_files.append(os.path.join(root, name))
    sch_files.sort()
    return sch_files


def export_project(board: pcbnew.BOARD) -> Dict[str, Any]:
    board_file = board.GetFileName()
    project_file = _find_project_file(board_file)
    project_dir = os.path.dirname(board_file)

    return {
        "project_path": project_dir,
        "board_file": board_file,
        "project_file": project_file,
        "kicad_version": _detect_kicad_version(),
        "project_variables": _read_project_variables(project_file),
        "schematic_files": _find_schematic_files(project_dir),
    }
