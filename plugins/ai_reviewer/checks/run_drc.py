import json
import os
import shutil
import subprocess
from typing import Any, Dict


def _resolve_kicad_cli() -> str:
    env_path = os.environ.get("KICAD_CLI", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path

    which_path = shutil.which("kicad-cli")
    if which_path:
        return which_path

    mac_bundle_path = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if os.path.exists(mac_bundle_path):
        return mac_bundle_path

    return "kicad-cli"


def _load_json(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_issue_count(report: Dict[str, Any]) -> int:
    if not isinstance(report, dict):
        return 0

    total = 0
    for key in ("violations", "issues", "errors", "warnings", "items"):
        value = report.get(key)
        if isinstance(value, list):
            total += len(value)

    summary = report.get("summary")
    if isinstance(summary, dict):
        for key in ("error_count", "errors", "warning_count", "warnings", "violations"):
            value = summary.get(key)
            if isinstance(value, int):
                total += value

    return total


def run_drc(board_file: str, output_dir: str) -> Dict[str, Any]:
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "drc_report.json")
    cli = _resolve_kicad_cli()
    cmd = [cli, "pcb", "drc", board_file, "--output", report_path, "--format", "json"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        report = _load_json(report_path)
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "command": cmd,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "report_path": report_path,
            "report_exists": bool(report),
            "issue_count": _extract_issue_count(report),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": 127,
            "command": cmd,
            "stdout": "",
            "stderr": "kicad-cli not found",
            "report_path": report_path,
            "report_exists": False,
            "issue_count": 0,
        }
