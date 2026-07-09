import os
from typing import Any, Dict, List


def _analyze_schematic_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            text = stream.read()
    except Exception:
        return {
            "path": path,
            "size_bytes": 0,
            "symbols_count": 0,
            "labels_count": 0,
            "hierarchical_labels_count": 0,
            "power_symbols_count": 0,
        }

    return {
        "path": path,
        "size_bytes": os.path.getsize(path),
        "symbols_count": text.count("(symbol "),
        "labels_count": text.count("(label "),
        "hierarchical_labels_count": text.count("(hierarchical_label "),
        "power_symbols_count": text.count("power:"),
    }


def export_schematic(project_data: Dict[str, Any]) -> Dict[str, Any]:
    files: List[str] = project_data.get("schematic_files", [])
    return {
        "files": [_analyze_schematic_file(path) for path in files],
        "is_multi_sheet": len(files) > 1,
    }
