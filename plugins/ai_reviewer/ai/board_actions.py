"""KiCad board read/write actions callable by the AI.

Every function returns a dict with at least:
    ok: bool
    message: str            — human-readable summary for the AI
    data: str (optional)    — detailed text for AI consumption
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_mm(value: Any) -> float:
    try:
        import pcbnew
        return float(pcbnew.ToMM(value))
    except Exception:
        return 0.0


def _from_mm(value: float) -> int:
    import pcbnew
    return int(pcbnew.FromMM(value))


def _get_severity_const(name: str):
    """Return pcbnew severity constant from name string ('error'/'warning'/'ignore')."""
    import pcbnew
    mapping = {
        "error":    getattr(pcbnew, "RPT_SEVERITY_ERROR",     2),
        "warning":  getattr(pcbnew, "RPT_SEVERITY_WARNING",   4),
        "ignore":   getattr(pcbnew, "RPT_SEVERITY_IGNORE",    8),
        "exclusion": getattr(pcbnew, "RPT_SEVERITY_EXCLUSION", 16),
    }
    return mapping.get(name.lower(), mapping["error"])


# Map user-friendly rule names to (pcbnew constant name, aliases)
_DRC_RULE_MAP = {
    "clearance":               ["DRCE_CLEARANCE", "DRCE_TOO_CLOSE_TRACKS", "DRCE_TOO_CLOSE_PADS"],
    "track_width":             ["DRCE_TRACK_WIDTH"],
    "min_hole":                ["DRCE_TOO_SMALL_DRILL", "DRCE_DRILLED_HOLES_TOO_CLOSE"],
    "copper_edge_clearance":   ["DRCE_COPPER_EDGE_CLEARANCE", "DRCE_EDGE_CLEARANCE"],
    "silk_over_copper":        ["DRCE_SILK_MASK_CLEARANCE", "DRCE_OVERLAPPING_SILK"],
    "net_conflict":            ["DRCE_NET_CONFLICT"],
    "diff_pair_gap":           ["DRCE_DIFF_PAIR_GAP_OUT_OF_RANGE"],
    "diff_pair_uncoupled":     ["DRCE_DIFF_PAIR_UNCOUPLED_LENGTH_TOO_LONG"],
    "courtyards_overlap":      ["DRCE_OVERLAPPING_FOOTPRINTS"],
    "starved_thermal":         ["DRCE_STARVED_THERMAL"],
    "unconnected_items":       ["DRCE_UNCONNECTED_ITEMS"],
    "footprint":               ["DRCE_FOOTPRINT", "DRCE_MISSING_COURTYARD"],
}


# ---------------------------------------------------------------------------
# read_design_rules
# ---------------------------------------------------------------------------

def read_design_rules() -> Dict[str, Any]:
    """Return detailed design settings: net classes, board minimums, DRC severities."""
    try:
        import pcbnew
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "message": "No active board."}

        settings = board.GetDesignSettings()
        lines = ["=== Design Rules ==="]

        # ── Net classes ──────────────────────────────────────────────
        lines.append("\n-- Net Classes --")
        try:
            netclasses = settings.GetNetClasses()
            default_nc = netclasses.GetDefault()
            if default_nc:
                lines.append(
                    f"  Default: clearance={_to_mm(default_nc.GetClearance()):.4f} mm"
                    f"  track_width={_to_mm(default_nc.GetTrackWidth()):.4f} mm"
                    f"  via_dia={_to_mm(default_nc.GetViaDiameter()):.4f} mm"
                    f"  via_drill={_to_mm(default_nc.GetViaDrill()):.4f} mm"
                )
            # Iterate custom net classes (KiCad 7-10 compat)
            try:
                for nc_name in list(netclasses.NetClassNames()):
                    if nc_name == "Default":
                        continue
                    nc = netclasses.Find(nc_name)
                    if nc:
                        lines.append(
                            f"  {nc_name}: clearance={_to_mm(nc.GetClearance()):.4f} mm"
                            f"  track_width={_to_mm(nc.GetTrackWidth()):.4f} mm"
                            f"  via_dia={_to_mm(nc.GetViaDiameter()):.4f} mm"
                        )
            except Exception:
                # Fallback iteration
                try:
                    for nc_pair in netclasses:
                        nc_name = nc_pair[0] if isinstance(nc_pair, (tuple, list)) else str(nc_pair)
                        lines.append(f"  {nc_name}")
                except Exception:
                    pass
        except Exception as exc:
            lines.append(f"  (Could not read net classes: {exc})")

        # ── Board minimums ───────────────────────────────────────────
        lines.append("\n-- Board Minimums --")
        minimums = {}
        for attr, label in [
            ("m_TrackMinWidth",      "min_track_width_mm"),
            ("m_MinHoleToHole",      "min_hole_to_hole_mm"),
            ("m_CopperEdgeClearanceValue", "copper_edge_clearance_mm"),
        ]:
            try:
                val = _to_mm(getattr(settings, attr))
                minimums[label] = val
                lines.append(f"  {label} = {val:.4f} mm")
            except Exception:
                pass

        # ── DRC severities ───────────────────────────────────────────
        lines.append("\n-- DRC Severities --")
        severities: Dict[str, str] = {}
        sev_names = {
            getattr(pcbnew, "RPT_SEVERITY_ERROR",     2):  "error",
            getattr(pcbnew, "RPT_SEVERITY_WARNING",   4):  "warning",
            getattr(pcbnew, "RPT_SEVERITY_IGNORE",    8):  "ignore",
        }
        try:
            for friendly_name, const_names in _DRC_RULE_MAP.items():
                for cname in const_names:
                    code = getattr(pcbnew, cname, None)
                    if code is None:
                        continue
                    try:
                        sev = settings.GetSeverity(code)
                        sev_str = sev_names.get(int(sev), str(sev))
                        severities[friendly_name] = sev_str
                        lines.append(f"  {friendly_name}: {sev_str}")
                        break
                    except Exception:
                        pass
        except Exception:
            lines.append("  (DRC severity API not available on this KiCad version)")

        data = "\n".join(lines)
        return {"ok": True, "message": "Design rules read successfully.", "data": data,
                "net_classes": [], "minimums": minimums, "severities": severities}

    except Exception as exc:
        return {"ok": False, "message": f"read_design_rules failed: {exc}"}


# ---------------------------------------------------------------------------
# modify_netclass
# ---------------------------------------------------------------------------

def modify_netclass(
    netclass_name: str,
    clearance_mm: Optional[float] = None,
    track_width_mm: Optional[float] = None,
    via_diameter_mm: Optional[float] = None,
    via_drill_mm: Optional[float] = None,
    uvia_diameter_mm: Optional[float] = None,
    uvia_drill_mm: Optional[float] = None,
) -> Dict[str, Any]:
    """Modify parameters of a net class on the active board."""
    try:
        import pcbnew
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "message": "No active board."}

        settings = board.GetDesignSettings()
        netclasses = settings.GetNetClasses()

        nc_name_lower = netclass_name.strip().lower()
        if nc_name_lower == "default":
            nc = netclasses.GetDefault()
        else:
            nc = netclasses.Find(netclass_name.strip())

        if nc is None:
            # List available names to help the AI
            available = []
            try:
                available = list(netclasses.NetClassNames())
            except Exception:
                pass
            return {
                "ok": False,
                "message": f"Net class '{netclass_name}' not found. "
                           f"Available: {available or ['Default']}",
            }

        changes: list = []

        if clearance_mm is not None:
            nc.SetClearance(_from_mm(clearance_mm))
            changes.append(f"clearance → {clearance_mm:.4f} mm")

        if track_width_mm is not None:
            nc.SetTrackWidth(_from_mm(track_width_mm))
            changes.append(f"track_width → {track_width_mm:.4f} mm")

        if via_diameter_mm is not None:
            nc.SetViaDiameter(_from_mm(via_diameter_mm))
            changes.append(f"via_diameter → {via_diameter_mm:.4f} mm")

        if via_drill_mm is not None:
            nc.SetViaDrill(_from_mm(via_drill_mm))
            changes.append(f"via_drill → {via_drill_mm:.4f} mm")

        if uvia_diameter_mm is not None:
            try:
                nc.SetuViaDiameter(_from_mm(uvia_diameter_mm))
                changes.append(f"uvia_diameter → {uvia_diameter_mm:.4f} mm")
            except Exception:
                pass

        if uvia_drill_mm is not None:
            try:
                nc.SetuViaDrill(_from_mm(uvia_drill_mm))
                changes.append(f"uvia_drill → {uvia_drill_mm:.4f} mm")
            except Exception:
                pass

        if not changes:
            return {"ok": False, "message": "No parameters specified to change."}

        board.SetModified()
        pcbnew.Refresh()

        summary = f"Net class '{netclass_name}' modified: " + ", ".join(changes)
        return {"ok": True, "message": summary, "data": summary}

    except Exception as exc:
        return {"ok": False, "message": f"modify_netclass failed: {exc}"}


# ---------------------------------------------------------------------------
# set_drc_severity
# ---------------------------------------------------------------------------

def set_drc_severity(violation_type: str, severity: str) -> Dict[str, Any]:
    """Change the severity (error/warning/ignore) of a DRC rule type.

    Falls back to writing a custom .kicad_dru rule if the native API is
    unavailable on the current KiCad version.
    """
    try:
        import pcbnew
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "message": "No active board."}

        const_names = _DRC_RULE_MAP.get(violation_type.lower())
        if not const_names:
            available = list(_DRC_RULE_MAP.keys())
            return {
                "ok": False,
                "message": f"Unknown violation type '{violation_type}'. "
                           f"Valid types: {available}",
            }

        sev_const = _get_severity_const(severity)
        settings = board.GetDesignSettings()

        applied = []
        # Try native API first
        for cname in const_names:
            code = getattr(pcbnew, cname, None)
            if code is None:
                continue
            try:
                # KiCad 7+ SetSeverity
                settings.SetSeverity(code, sev_const)
                applied.append(cname)
            except AttributeError:
                # Fallback: try direct dict access
                try:
                    settings.m_DRCSeverities[code] = sev_const
                    applied.append(cname)
                except Exception:
                    pass

        if applied:
            board.SetModified()
            pcbnew.Refresh()
            return {
                "ok": True,
                "message": f"DRC severity for '{violation_type}' set to '{severity}' "
                           f"(rules: {applied}). Run DRC again to see the effect.",
                "data": f"Applied to: {applied}",
            }

        # Fallback: write a .kicad_dru custom rule
        return _set_drc_severity_via_dru(board, violation_type, severity)

    except Exception as exc:
        return {"ok": False, "message": f"set_drc_severity failed: {exc}"}


def _set_drc_severity_via_dru(board, violation_type: str, severity: str) -> Dict[str, Any]:
    """Write/update a .kicad_dru custom design rule to override severity."""
    try:
        import os, re as _re
        board_file = board.GetFileName()
        if not board_file:
            return {"ok": False, "message": "Board has no file path — save the board first."}

        dru_path = os.path.splitext(board_file)[0] + ".kicad_dru"

        # Map violation type to DRU constraint type
        _constraint_map = {
            "clearance":              "clearance",
            "track_width":            "track_width",
            "min_hole":               "hole_size",
            "copper_edge_clearance":  "edge_clearance",
            "silk_over_copper":       "silk_clearance",
            "courtyards_overlap":     "courtyard_clearance",
        }
        constraint = _constraint_map.get(violation_type.lower())
        if constraint is None:
            return {
                "ok": False,
                "message": f"Constraint type for '{violation_type}' is not supported "
                           "via .kicad_dru rules in this KiCad version.",
            }

        rule_name = f"maelectrix_{violation_type}_severity"
        new_rule = (
            f"\n(rule {rule_name}\n"
            f"   (constraint {constraint})\n"
            f"   (severity {severity})\n"
            f")\n"
        )

        existing = ""
        if os.path.exists(dru_path):
            with open(dru_path, "r", encoding="utf-8") as fh:
                existing = fh.read()

        # Remove existing Maelectrix-generated rule for this type
        existing = _re.sub(
            r"\n\(rule " + _re.escape(rule_name) + r"[^)]*(?:\([^)]*\)\s*)*\)\s*\n",
            "\n",
            existing,
        )

        with open(dru_path, "w", encoding="utf-8") as fh:
            fh.write(existing.rstrip() + new_rule)

        return {
            "ok": True,
            "message": f"Custom DRC rule written to {os.path.basename(dru_path)}: "
                       f"'{violation_type}' severity → '{severity}'. "
                       "Reload the board or run DRC to apply.",
            "data": f"Rule '{rule_name}' written to {dru_path}",
        }
    except Exception as exc:
        return {"ok": False, "message": f"_set_drc_severity_via_dru failed: {exc}"}


# ---------------------------------------------------------------------------
# save_board
# ---------------------------------------------------------------------------

def save_board() -> Dict[str, Any]:
    """Save the current board to its file, persisting all in-memory modifications."""
    try:
        import pcbnew
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "message": "No active board."}
        path = board.GetFileName()
        if not path:
            return {"ok": False, "message": "Board has no file path — use File → Save As first."}
        board.Save(path)
        return {"ok": True, "message": f"Board saved to {path}.", "data": f"Saved: {path}"}
    except Exception as exc:
        return {"ok": False, "message": f"save_board failed: {exc}"}


# ---------------------------------------------------------------------------
# read_net_class_assignments
# ---------------------------------------------------------------------------

def read_net_class_assignments() -> Dict[str, Any]:
    """List which nets belong to which net class."""
    try:
        import pcbnew
        board = pcbnew.GetBoard()
        if board is None:
            return {"ok": False, "message": "No active board."}

        settings = board.GetDesignSettings()
        lines = ["Net → Net Class assignments:"]
        try:
            netclasses = settings.GetNetClasses()
            for net_info in board.GetNetInfo().NetsByName().values():
                net_name = net_info.GetNetname()
                if not net_name or net_name == "":
                    continue
                # Find which class this net belongs to
                nc_name = "Default"
                try:
                    assigned = netclasses.GetEffectiveNetClass(net_name)
                    if assigned:
                        nc_name = assigned.GetName()
                except Exception:
                    pass
                lines.append(f"  {net_name} → {nc_name}")
        except Exception as exc:
            lines.append(f"(Error reading assignments: {exc})")

        data = "\n".join(lines[:200])  # Limit output
        return {"ok": True, "message": f"{len(lines)-1} net assignments read.", "data": data}
    except Exception as exc:
        return {"ok": False, "message": f"read_net_class_assignments failed: {exc}"}


# ---------------------------------------------------------------------------
# execute_pcbnew_script
# ---------------------------------------------------------------------------

def execute_pcbnew_script(code: str) -> Dict[str, Any]:
    """Execute arbitrary Python code in KiCad's live scripting context.

    Pre-injected globals: ``board`` (pcbnew.BOARD) and ``pcbnew`` (module).
    stdout/stderr are captured and returned in ``data``.
    pcbnew.Refresh() is called automatically on success.
    """
    import io
    import sys
    import traceback as _tb

    if not code or not code.strip():
        return {"ok": False, "message": "No code provided."}

    try:
        import pcbnew
    except ImportError:
        return {"ok": False, "message": "pcbnew module not available."}

    board = pcbnew.GetBoard()
    if board is None:
        return {"ok": False, "message": "No active board."}

    old_stdout, old_stderr = sys.stdout, sys.stderr
    cap_out, cap_err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = cap_out, cap_err

    exec_error: Optional[str] = None
    try:
        exec_globals: Dict[str, Any] = {
            "pcbnew": pcbnew,
            "board": board,
            "__builtins__": __builtins__,
        }
        exec(code, exec_globals)  # noqa: S102
    except Exception:
        exec_error = _tb.format_exc()
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    stdout_text = cap_out.getvalue()
    stderr_text = cap_err.getvalue()

    if exec_error:
        parts = []
        if stdout_text:
            parts.append(f"stdout:\n{stdout_text}")
        parts.append(f"error:\n{exec_error}")
        if stderr_text:
            parts.append(f"stderr:\n{stderr_text}")
        return {"ok": False, "message": "Script raised an exception.", "data": "\n".join(parts)}

    # Refresh the KiCad display to reflect changes
    try:
        pcbnew.Refresh()
    except Exception:
        pass

    parts = []
    if stdout_text:
        parts.append(stdout_text.rstrip())
    if stderr_text:
        parts.append(f"(stderr): {stderr_text.rstrip()}")
    summary = "\n".join(parts) if parts else "Script executed — no output."
    return {"ok": True, "message": "Script executed successfully.", "data": summary}

