from typing import Any, Dict, List

import pcbnew


def _to_mm(value: Any) -> float:
    try:
        return float(pcbnew.ToMM(value))
    except Exception:
        return 0.0


def _extract_board_size(board: pcbnew.BOARD) -> Dict[str, float]:
    if hasattr(board, "GetBoardEdgesBoundingBox"):
        try:
            bbox = board.GetBoardEdgesBoundingBox()
            return {
                "width_mm": _to_mm(bbox.GetWidth()),
                "height_mm": _to_mm(bbox.GetHeight()),
            }
        except Exception:
            pass
    return {"width_mm": 0.0, "height_mm": 0.0}


def export_pcb(board: pcbnew.BOARD) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "dimensions": _extract_board_size(board),
        "layers": [],
        "footprints": [],
        "pads": [],
        "tracks": [],
        "vias": [],
        "zones": [],
        "nets": [],
    }

    for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
        name = board.GetLayerName(layer_id)
        if not name:
            continue
        data["layers"].append(
            {
                "id": layer_id,
                "name": name,
                "enabled": bool(board.IsLayerEnabled(layer_id)),
            }
        )

    for fp in board.GetFootprints():
        fp_ref = fp.GetReference()
        fp_data = {
            "ref": fp_ref,
            "value": fp.GetValue(),
            "footprint": fp.GetFPIDAsString() if hasattr(fp, "GetFPIDAsString") else "",
            "layer": board.GetLayerName(fp.GetLayer()),
            "x_mm": _to_mm(fp.GetPosition().x),
            "y_mm": _to_mm(fp.GetPosition().y),
            "orientation_deg": fp.GetOrientationDegrees() if hasattr(fp, "GetOrientationDegrees") else 0.0,
        }
        data["footprints"].append(fp_data)

        for pad in fp.Pads():
            net_name = pad.GetNetname() if hasattr(pad, "GetNetname") else ""
            data["pads"].append(
                {
                    "id": f"{fp_ref}:{pad.GetNumber()}",
                    "footprint_ref": fp_ref,
                    "number": pad.GetNumber(),
                    "net": net_name,
                    "layer": board.GetLayerName(pad.GetLayer()) if hasattr(pad, "GetLayer") else "",
                    "x_mm": _to_mm(pad.GetPosition().x),
                    "y_mm": _to_mm(pad.GetPosition().y),
                    "size_x_mm": _to_mm(pad.GetSize().x) if hasattr(pad, "GetSize") else 0.0,
                    "size_y_mm": _to_mm(pad.GetSize().y) if hasattr(pad, "GetSize") else 0.0,
                }
            )

    for item in board.GetTracks():
        common = {
            "net": item.GetNetname() if hasattr(item, "GetNetname") else "",
            "layer": board.GetLayerName(item.GetLayer()) if hasattr(item, "GetLayer") else "",
            "width_mm": _to_mm(item.GetWidth()) if hasattr(item, "GetWidth") else 0.0,
            "length_mm": _to_mm(item.GetLength()) if hasattr(item, "GetLength") else 0.0,
        }
        if hasattr(item, "GetDrillValue"):
            common["drill_mm"] = _to_mm(item.GetDrillValue())
            common["x_mm"] = _to_mm(item.GetPosition().x) if hasattr(item, "GetPosition") else 0.0
            common["y_mm"] = _to_mm(item.GetPosition().y) if hasattr(item, "GetPosition") else 0.0
            data["vias"].append(common)
        else:
            data["tracks"].append(common)

    if hasattr(board, "Zones"):
        for zone in board.Zones():
            data["zones"].append(
                {
                    "net": zone.GetNetname() if hasattr(zone, "GetNetname") else "",
                    "layer": board.GetLayerName(zone.GetLayer()) if hasattr(zone, "GetLayer") else "",
                    "is_filled": bool(zone.IsFilled()) if hasattr(zone, "IsFilled") else False,
                    "priority": int(zone.GetAssignedPriority()) if hasattr(zone, "GetAssignedPriority") else 0,
                }
            )

    for netcode in board.GetNetsByNetcode():
        net = board.FindNet(netcode)
        if not net:
            continue
        data["nets"].append(
            {
                "code": int(netcode),
                "name": net.GetNetname(),
            }
        )

    return data
