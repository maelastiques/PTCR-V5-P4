"""Export PCB layers as PNG images via pcbnew PLOT_CONTROLLER + wxPython SVG conversion."""

from __future__ import annotations

import base64
import os
from typing import Dict, List, Optional, Tuple


# (pcbnew attr name, file suffix, human description)
# These match pdf_exporter.py layer naming conventions.
_VISION_LAYERS: List[Tuple[str, str, str]] = [
    ("F_Cu",     "F_Cu",         "Front Copper"),
    ("B_Cu",     "B_Cu",         "Back Copper"),
    ("F_SilkS",  "F_Silkscreen", "Front Silkscreen"),
    ("B_SilkS",  "B_Silkscreen", "Back Silkscreen"),
    ("F_Mask",   "F_Mask",       "Front Mask"),
    ("B_Mask",   "B_Mask",       "Back Mask"),
    ("Edge_Cuts","Edge_Cuts",    "Board Outline"),
    ("F_Fab",    "F_Fab",        "Front Fab"),
    ("B_Fab",    "B_Fab",        "Back Fab"),
    ("F_CrtYd",  "F_Courtyard",  "Front Courtyard"),
]


# Stores errors from the last export_layer_images call for diagnostic reporting.
_last_export_errors: List[str] = []


def _svg_to_png(svg_path: str, png_path: str, size: int = 1024) -> Optional[str]:
    """Convert an SVG file to a PNG file using wxPython BitmapBundle.

    Returns *png_path* on success, or *None* if conversion fails.
    Appends diagnostic info to *_last_export_errors* on failure.
    """
    try:
        import wx  # type: ignore[import]
        bundle = wx.BitmapBundle.FromSVGFile(svg_path, wx.Size(size, size))
        bmp = bundle.GetBitmap(wx.Size(size, size))
        if not bmp.IsOk():
            _last_export_errors.append(
                f"SVG→PNG: bitmap not ok for {os.path.basename(svg_path)}"
            )
            return None
        img = bmp.ConvertToImage()
        if img.SaveFile(png_path, wx.BITMAP_TYPE_PNG):
            return png_path
        _last_export_errors.append(
            f"SVG→PNG: SaveFile failed for {os.path.basename(png_path)}"
        )
        return None
    except Exception as exc:
        _last_export_errors.append(
            f"SVG→PNG: exception converting {os.path.basename(svg_path)}: {exc}"
        )
        return None


def export_layer_images(
    board,  # pcbnew.BOARD object (or a board file path string as legacy fallback)
    output_dir: str,
    ppi: int = 150,       # kept for API compatibility; SVG export is vector
    max_layers: int = 6,
) -> Dict[str, str]:
    """Export PCB layers as PNG images using pcbnew PLOT_CONTROLLER.

    Uses the live in-memory board so unsaved changes are captured.  SVG is
    produced first via PLOT_CONTROLLER, then converted to PNG with wxPython.

    Returns:
        {description: absolute_png_path} for each successfully exported layer.
        On failure returns an empty dict; errors in *_last_export_errors*.
    """
    global _last_export_errors
    _last_export_errors = []

    import pcbnew  # type: ignore[import]

    # Accept legacy string path: resolve to live board object if possible.
    if isinstance(board, str):
        live = pcbnew.GetBoard()
        if live is not None:
            board = live
        else:
            _last_export_errors.append(
                "export_layer_images: no live board; cannot export without a board object."
            )
            return {}

    if board is None:
        _last_export_errors.append("export_layer_images: board is None.")
        return {}

    os.makedirs(output_dir, exist_ok=True)

    pctl = pcbnew.PLOT_CONTROLLER(board)
    popt = pctl.GetPlotOptions()
    popt.SetOutputDirectory(output_dir)
    popt.SetPlotFrameRef(False)
    popt.SetAutoScale(False)
    popt.SetScale(1)
    popt.SetMirror(False)
    try:
        popt.SetDrillMarksType(pcbnew.PCB_PLOT_PARAMS.NO_DRILL_SHAPE)
    except Exception:
        pass

    results: Dict[str, str] = {}

    for attr_name, suffix, description in _VISION_LAYERS[:max_layers]:
        layer_id = getattr(pcbnew, attr_name, None)
        if layer_id is None:
            _last_export_errors.append(
                f"[{suffix}] unknown pcbnew constant '{attr_name}'"
            )
            continue
        try:
            if not board.IsLayerEnabled(layer_id):
                continue
            pctl.OpenPlotfile(suffix, pcbnew.PLOT_FORMAT_SVG, description)
            pctl.SetColorMode(True)
            pctl.PlotLayer()
            pctl.ClosePlot()
            svg_path = pctl.GetPlotFileName()
            if not svg_path or not os.path.exists(svg_path):
                _last_export_errors.append(f"[{suffix}] SVG file not created")
                continue
            png_path = os.path.join(output_dir, f"{suffix}.png")
            converted = _svg_to_png(svg_path, png_path)
            try:
                os.unlink(svg_path)
            except Exception:
                pass
            if converted:
                results[description] = converted
            else:
                # Keep SVG path as fallback for display in WebView
                results[description] = svg_path if os.path.exists(svg_path) else ""
        except Exception as exc:
            _last_export_errors.append(f"[{suffix}] exception: {exc}")
            try:
                pctl.ClosePlot()
            except Exception:
                pass

    return {k: v for k, v in results.items() if v}


def images_to_base64(image_paths: Dict[str, str]) -> Dict[str, str]:
    """
    Read each PNG file and return base64-encoded content.

    Returns:
        {layer_name: base64_string} suitable for OpenAI vision API.
    """
    b64_data: Dict[str, str] = {}
    for layer_name, path in image_paths.items():
        try:
            with open(path, "rb") as fh:
                b64_data[layer_name] = base64.b64encode(fh.read()).decode("ascii")
        except Exception:
            pass
    return b64_data


def describe_exported_images(image_paths: Dict[str, str]) -> str:
    """Human-readable summary of exported images for AI context."""
    if not image_paths:
        error_detail = ""
        if _last_export_errors:
            error_detail = "\nExport errors:\n" + "\n".join(f"  {e}" for e in _last_export_errors[:4])
        return (
            "Layer image export failed — no PNG files were produced. "
            "Reason: kicad-cli could not render the board (may be a conflict with "
            "the running KiCad instance, a missing display, or an unsupported flag)."
            + error_detail
        )
    lines = [f"Layer images exported ({len(image_paths)} layers):"]
    for layer_name, path in image_paths.items():
        size_kb = os.path.getsize(path) // 1024 if os.path.exists(path) else 0
        lines.append(f"  • {layer_name:<20} {size_kb} KB  →  {path}")
    lines.append("Images are attached to this message for visual analysis.")
    return "\n".join(lines)
