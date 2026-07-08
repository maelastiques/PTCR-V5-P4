from typing import Any, Dict

import pcbnew


def _to_mm(value: Any) -> float:
    try:
        return float(pcbnew.ToMM(value))
    except Exception:
        return 0.0


def export_stackup(board: pcbnew.BOARD) -> Dict[str, Any]:
    settings = board.GetDesignSettings() if hasattr(board, "GetDesignSettings") else None
    thickness = 0.0
    if settings and hasattr(settings, "GetBoardThickness"):
        try:
            thickness = _to_mm(settings.GetBoardThickness())
        except Exception:
            thickness = 0.0

    layers = []
    for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
        name = board.GetLayerName(layer_id)
        if not name:
            continue
        layers.append(
            {
                "id": layer_id,
                "name": name,
                "enabled": bool(board.IsLayerEnabled(layer_id)),
            }
        )

    return {
        "copper_layers": int(board.GetCopperLayerCount()) if hasattr(board, "GetCopperLayerCount") else 0,
        "board_thickness_mm": thickness,
        "layers": layers,
    }
