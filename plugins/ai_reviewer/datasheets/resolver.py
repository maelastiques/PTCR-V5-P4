from typing import Any, Dict, List


def resolve_components_for_datasheets(footprints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resolved = []
    for fp in footprints:
        resolved.append(
            {
                "ref": fp.get("ref", ""),
                "value": fp.get("value", ""),
                "candidate_queries": [fp.get("value", ""), fp.get("footprint", "")],
                "status": "pending",
            }
        )
    return resolved
