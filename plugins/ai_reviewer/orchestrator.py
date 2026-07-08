import json
import os
from typing import Any, Dict, List, Tuple

import pcbnew

from .ai.review_engine import run_review
from .checks.run_drc import run_drc
from .checks.run_erc import run_erc
from .datasheets.downloader import fetch_datasheets
from .datasheets.parser import parse_datasheet_corpus
from .datasheets.resolver import resolve_components_for_datasheets
from .exporters.graph_builder import build_project_graph
from .exporters.pcb_exporter import export_pcb
from .exporters.project_exporter import export_project
from .exporters.schematic_exporter import export_schematic
from .exporters.stackup_exporter import export_stackup
from .ui.settings import PluginSettings


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)


def _read_json(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def run_full_review(board: pcbnew.BOARD) -> Tuple[Dict[str, Any], List[str]]:
    settings = PluginSettings()

    project = export_project(board)
    project_dir = project.get("project_path", os.path.dirname(board.GetFileName()))
    context_dir = settings.context_dir(project_dir)
    datasheets_dir = os.path.join(context_dir, "datasheets")
    os.makedirs(context_dir, exist_ok=True)

    pcb = export_pcb(board)
    schematic = export_schematic(project)
    stackup = export_stackup(board)

    drc = run_drc(board.GetFileName(), context_dir)

    schematic_files = project.get("schematic_files", [])
    erc = {
        "ok": False,
        "returncode": -1,
        "stderr": "No schematic file found",
        "report_path": os.path.join(context_dir, "erc_report.json"),
        "report_exists": False,
        "issue_count": 0,
    }
    if schematic_files:
        erc = run_erc(schematic_files[0], context_dir)

    drc_report = _read_json(drc.get("report_path", ""))
    erc_report = _read_json(erc.get("report_path", ""))

    datasheet_candidates = resolve_components_for_datasheets(pcb.get("footprints", []))
    datasheet_entries = fetch_datasheets(datasheet_candidates, datasheets_dir)
    datasheets = parse_datasheet_corpus(datasheet_entries)

    graph = build_project_graph(project, pcb, schematic)

    context = {
        "project": project,
        "pcb": pcb,
        "schematic": schematic,
        "stackup": stackup,
        "drc": drc,
        "erc": erc,
        "drc_report": drc_report,
        "erc_report": erc_report,
        "datasheets": datasheets,
        "graph": graph,
    }

    review = run_review(context)

    files = {
        "project.json": project,
        "pcb.json": pcb,
        "schematic.json": schematic,
        "stackup.json": stackup,
        "drc.json": drc_report,
        "erc.json": erc_report,
        "drc_run.json": drc,
        "erc_run.json": erc,
        "datasheets.json": datasheets,
        "graph.json": graph,
        "review.json": review,
    }

    generated_paths: List[str] = []
    for name, payload in files.items():
        path = os.path.join(context_dir, name)
        _write_json(path, payload)
        generated_paths.append(path)

    return review, generated_paths
