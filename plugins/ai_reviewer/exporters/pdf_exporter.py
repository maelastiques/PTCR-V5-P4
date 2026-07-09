"""Export each active PCB layer as a separate PDF using KiCad's PLOT_CONTROLLER."""

from __future__ import annotations

import os
from typing import List, Tuple


# (pcbnew layer id attr, file suffix, human description)
_LAYERS: List[Tuple[str, str, str]] = [
    ("F_Cu",     "F_Cu",         "Front Copper"),
    ("In1_Cu",   "In1_Cu",       "Inner Copper 1"),
    ("In2_Cu",   "In2_Cu",       "Inner Copper 2"),
    ("In3_Cu",   "In3_Cu",       "Inner Copper 3"),
    ("In4_Cu",   "In4_Cu",       "Inner Copper 4"),
    ("In5_Cu",   "In5_Cu",       "Inner Copper 5"),
    ("In6_Cu",   "In6_Cu",       "Inner Copper 6"),
    ("B_Cu",     "B_Cu",         "Back Copper"),
    ("F_Paste",  "F_Paste",      "Front Paste"),
    ("B_Paste",  "B_Paste",      "Back Paste"),
    ("F_SilkS",  "F_Silkscreen", "Front Silkscreen"),
    ("B_SilkS",  "B_Silkscreen", "Back Silkscreen"),
    ("F_Mask",   "F_Mask",       "Front Mask"),
    ("B_Mask",   "B_Mask",       "Back Mask"),
    ("Dwgs_User","Drawings",     "Drawings"),
    ("Edge_Cuts","Edge_Cuts",    "Board Outline"),
    ("Margin",   "Margin",       "Margin"),
    ("F_CrtYd",  "F_Courtyard",  "Front Courtyard"),
    ("B_CrtYd",  "B_Courtyard",  "Back Courtyard"),
    ("F_Fab",    "F_Fab",        "Front Fab"),
    ("B_Fab",    "B_Fab",        "Back Fab"),
]


def export_layers_pdf(board, output_dir: str) -> List[str]:
    """
    Export each active layer on the board as a PDF file.

    Returns a list of absolute paths to the files that were successfully created.
    Requires a live pcbnew.BOARD object (not a file path).
    """
    import pcbnew  # type: ignore[import]

    os.makedirs(output_dir, exist_ok=True)

    pctl = pcbnew.PLOT_CONTROLLER(board)
    popt = pctl.GetPlotOptions()
    popt.SetOutputDirectory(output_dir)
    popt.SetPlotFrameRef(False)
    popt.SetAutoScale(False)
    popt.SetScale(1)
    popt.SetMirror(False)
    popt.SetUseGerberAttributes(False)
    try:
        popt.SetDrillMarksType(pcbnew.PCB_PLOT_PARAMS.NO_DRILL_SHAPE)
    except Exception:
        try:
            popt.SetDrillMarksType(0)
        except Exception:
            pass

    exported: List[str] = []

    for attr_name, suffix, description in _LAYERS:
        layer_id = getattr(pcbnew, attr_name, None)
        if layer_id is None:
            continue
        try:
            if not board.IsLayerEnabled(layer_id):
                continue
            pctl.OpenPlotfile(suffix, pcbnew.PLOT_FORMAT_PDF, description)
            pctl.SetColorMode(True)
            pctl.PlotLayer()
            pctl.ClosePlot()
            fname = pctl.GetPlotFileName()
            if fname and os.path.exists(fname):
                exported.append(fname)
        except Exception:
            try:
                pctl.ClosePlot()
            except Exception:
                pass

    return exported
