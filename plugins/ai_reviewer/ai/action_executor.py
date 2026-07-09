import json
import os
from typing import Any, Dict, List, Optional

import pcbnew

from ..checks.run_drc import run_drc
from ..checks.run_erc import run_erc
from ..exporters.project_exporter import export_project
from ..orchestrator import run_full_review
from . import board_actions
from .ultralibrarian import get_part_details, lookup_part, search_parts


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_schematic_file(board: pcbnew.BOARD, context_dir: str) -> str:
    project_data = export_project(board)
    files = project_data.get("schematic_files", [])
    if files:
        return str(files[0])

    project_json = _read_json(os.path.join(context_dir, "project.json"))
    files = project_json.get("schematic_files", []) if isinstance(project_json, dict) else []
    return str(files[0]) if files else ""


def _format_components_for_ai(pcb_data: Dict[str, Any]) -> str:
    """Return a compact, readable component list for injection into the AI context."""
    footprints = pcb_data.get("footprints", [])
    if not footprints:
        return "No components found in PCB data."
    lines = [f"{len(footprints)} components on the PCB:"]
    for fp in footprints:
        ref = fp.get("ref", "?")
        value = fp.get("value", "?")
        footprint = fp.get("footprint", "?")
        x = fp.get("x_mm", 0)
        y = fp.get("y_mm", 0)
        layer = fp.get("layer", "F.Cu")
        lines.append(f"  {ref}: {value}  ({footprint})  [{layer}  {x:.1f},{y:.1f} mm]")
    return "\n".join(lines)


def _format_drc_for_ai(report: Dict[str, Any]) -> str:
    violations = report.get("violations", []) or report.get("errors", [])
    if not violations:
        return "DRC: no violations found."
    lines = [f"DRC violations ({len(violations)} total):"]
    for v in violations[:60]:
        desc = v.get("description", "") or v.get("message", "")
        rule = v.get("rule_name", "") or v.get("type", "")
        lines.append(f"  [{rule}] {desc}")
    return "\n".join(lines)


def _format_erc_for_ai(report: Dict[str, Any]) -> str:
    violations = report.get("violations", []) or report.get("errors", [])
    if not violations:
        return "ERC: no violations found."
    lines = [f"ERC violations ({len(violations)} total):"]
    for v in violations[:60]:
        desc = v.get("description", "") or v.get("message", "")
        rule = v.get("rule_name", "") or v.get("type", "")
        lines.append(f"  [{rule}] {desc}")
    return "\n".join(lines)


def execute_action(action: str, board_file: str, context_dir: str,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    os.makedirs(context_dir, exist_ok=True)
    params = params or {}

    if action == "execute_full":
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "message": "No active KiCad board found."}
        review, files = run_full_review(board)
        # Also export PCB data so subsequent read_pcb has it available
        from ..exporters.pcb_exporter import export_pcb as _export_pcb
        pcb_data = _export_pcb(board)
        _write_json(os.path.join(context_dir, "pcb.json"), pcb_data)
        comp_summary = _format_components_for_ai(pcb_data)
        return {
            "ok": True,
            "action": action,
            "message": f"Full export done. {len(pcb_data.get('footprints', []))} components.",
            "data": comp_summary,
            "findings": review.get("findings", []),
        }

    if action in ("read_project", "read_pcb"):
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "action": action, "message": "No active KiCad board found."}
        from ..exporters.pcb_exporter import export_pcb as _export_pcb
        from ..exporters.project_exporter import export_project as _export_project
        project_data = _export_project(board)
        pcb_data = _export_pcb(board)
        _write_json(os.path.join(context_dir, "project.json"), project_data)
        _write_json(os.path.join(context_dir, "pcb.json"), pcb_data)
        comp_summary = _format_components_for_ai(pcb_data)
        net_list = ", ".join(
            n.get("name", "") for n in pcb_data.get("nets", []) if n.get("name", "").strip()
        )[:2000]
        data = comp_summary + (f"\n\nNets: {net_list}" if net_list else "")
        return {
            "ok": True,
            "action": action,
            "message": f"{len(pcb_data.get('footprints', []))} components loaded.",
            "data": data,
        }

    if action == "execute_drc":
        result = run_drc(board_file, context_dir)
        _write_json(os.path.join(context_dir, "drc_run.json"), result)
        report = _read_json(result.get("report_path", ""))
        _write_json(os.path.join(context_dir, "drc.json"), report)
        drc_summary = _format_drc_for_ai(report)
        return {
            "ok": bool(result.get("ok", False)),
            "action": action,
            "message": f"DRC done: {result.get('issue_count', 0)} violations.",
            "data": drc_summary,
            "returncode": result.get("returncode", -1),
            "issue_count": result.get("issue_count", 0),
            "stderr": result.get("stderr", ""),
        }

    if action in ("execute_erc", "run_erc"):
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "action": action, "message": "No active KiCad board found."}

        schematic_file = _find_schematic_file(board, context_dir)
        if not schematic_file:
            return {"ok": False, "action": action, "message": "No schematic file found."}

        result = run_erc(schematic_file, context_dir)
        _write_json(os.path.join(context_dir, "erc_run.json"), result)
        report = _read_json(result.get("report_path", ""))
        _write_json(os.path.join(context_dir, "erc.json"), report)
        erc_summary = _format_erc_for_ai(report)
        return {
            "ok": bool(result.get("ok", False)),
            "action": action,
            "message": f"ERC done: {result.get('issue_count', 0)} violations.",
            "data": erc_summary,
            "returncode": result.get("returncode", -1),
            "issue_count": result.get("issue_count", 0),
            "stderr": result.get("stderr", ""),
        }

    if action == "export_pdf":
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "action": action, "message": "No active KiCad board found."}
        pdf_dir = os.path.join(context_dir, "pdf_export")
        from ..exporters.pdf_exporter import export_layers_pdf
        files = export_layers_pdf(board, pdf_dir)
        file_list = "\n".join(f"  • {os.path.basename(f)}" for f in files) or "  (none)"
        return {
            "ok": bool(files),
            "action": action,
            "message": f"Exported {len(files)} PDF layer files.",
            "data": (
                f"PDF export complete — {len(files)} layers saved to:\n  {pdf_dir}\n\n"
                f"Files:\n{file_list}"
            ),
            "files": files,
            "output_dir": pdf_dir,
        }

    if action == "export_images":
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "action": action, "message": "No active KiCad board found."}
        img_dir = os.path.join(context_dir, "layer_images")
        from ..exporters.image_exporter import (
            export_layer_images,
            images_to_base64,
            describe_exported_images,
        )
        image_paths = export_layer_images(board, img_dir)
        b64 = images_to_base64(image_paths) if image_paths else {}
        return {
            "ok": bool(image_paths),
            "action": action,
            "message": f"Exported {len(image_paths)} layer images.",
            "data": describe_exported_images(image_paths),
            "image_b64": b64,
            "image_paths": list(image_paths.values()),
        }

    if action == "ultralibrarian_search":
        query = str(params.get("query", "")).strip()
        if not query:
            return {"ok": False, "action": action, "message": "Missing query."}
        limit = int(params.get("limit", 5) or 5)
        result = search_parts(query=query, limit=limit)
        _write_json(os.path.join(context_dir, "ultralibrarian.json"), result)
        return {
            "ok": bool(result.get("ok", False)),
            "action": action,
            "message": f"UltraLibrarian search done: {result.get('count', 0)} results.",
            "data": json.dumps(result, ensure_ascii=False, indent=2),
            "result": result,
        }

    if action == "ultralibrarian_get_part":
        detail_url = str(params.get("detail_url", "")).strip()
        if not detail_url:
            return {"ok": False, "action": action, "message": "Missing detail_url."}
        result = get_part_details(detail_url=detail_url)
        _write_json(os.path.join(context_dir, "ultralibrarian.json"), result)
        return {
            "ok": bool(result.get("ok", False)),
            "action": action,
            "message": "UltraLibrarian part details loaded.",
            "data": json.dumps(result, ensure_ascii=False, indent=2),
            "result": result,
        }

    if action == "ultralibrarian_lookup":
        query = str(params.get("query", "")).strip()
        if not query:
            return {"ok": False, "action": action, "message": "Missing query."}
        limit = int(params.get("limit", 5) or 5)
        result = lookup_part(query=query, limit=limit)
        _write_json(os.path.join(context_dir, "ultralibrarian.json"), result)
        return {
            "ok": bool(result.get("ok", False)),
            "action": action,
            "message": "UltraLibrarian lookup done.",
            "data": json.dumps(result, ensure_ascii=False, indent=2),
            "result": result,
        }

    # ── Design-rule read actions ──────────────────────────────────────────────
    if action == "read_design_rules":
        return board_actions.read_design_rules()

    if action == "read_net_class_assignments":
        return board_actions.read_net_class_assignments()

    # ── Write actions ─────────────────────────────────────────────────────────
    if action == "modify_netclass":
        netclass_name = params.get("netclass_name", "Default")
        return board_actions.modify_netclass(
            netclass_name=netclass_name,
            clearance_mm=params.get("clearance_mm"),
            track_width_mm=params.get("track_width_mm"),
            via_diameter_mm=params.get("via_diameter_mm"),
            via_drill_mm=params.get("via_drill_mm"),
            uvia_diameter_mm=params.get("uvia_diameter_mm"),
            uvia_drill_mm=params.get("uvia_drill_mm"),
        )

    if action == "set_drc_severity":
        return board_actions.set_drc_severity(
            violation_type=str(params.get("violation_type", "")),
            severity=str(params.get("severity", "error")),
        )

    if action == "save_board":
        return board_actions.save_board()

    if action == "execute_pcbnew_script":
        return board_actions.execute_pcbnew_script(
            code=str(params.get("code", ""))
        )

    return {
        "ok": False,
        "action": action,
        "message": f"Unknown action '{action}'.",
    }
