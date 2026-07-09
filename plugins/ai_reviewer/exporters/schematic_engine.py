"""
KiCad-style schematic auto-layout and SVG rendering from a SPICE-like netlist.

Netlist syntax (one component per line, # comments):
    Ref  Lib:Symbol  Value  pin1_net  pin2_net  [pin3_net ...]
    label  net_name  display_text    (optional display name for a net)

Power nets are auto-detected by name:
    VCC, VDD, +5V, +3V3, +3.3V, VBAT, VSUP, AVCC, DVCC …  → power source (top)
    GND, AGND, DGND, VSS, GND_*, EARTH, 0V                 → ground (bottom)

Example:
    R1  Device:R  10k   VCC   base
    R2  Device:R  47k   base  GND
    C1  Device:C  100nF base  GND
    label base Vbase
"""

from __future__ import annotations

import html as _html
import math
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .sym_renderer import (
    get_pin_positions, render_symbol_group,
    get_generic_pin_positions, render_generic_ic,
    load_custom_symbol, save_custom_symbol,
    render_custom_ic, get_custom_pin_positions,
)

# ---------------------------------------------------------------------------
# Theme — KiCad default light palette (fixed regardless of UI theme)
# ---------------------------------------------------------------------------
C_BG      = "#f5f4ef"   # background          (off-white, KiCad default)
C_WIRE    = "#009600"   # wires & junctions   (green)
C_BODY    = "#840000"   # (unused here — sym_renderer handles outlines)
C_REF     = "#006464"   # reference labels    (teal)
C_VAL     = "#006464"   # value labels        (teal)
C_LABEL   = "#0f0f0f"   # net labels          (near-black, like label_local)
C_GRID    = "#cbc9c2"   # grid               (light gray on off-white)
C_JUNC    = "#009600"   # junction circles    (green)
C_PWR     = "#009600"   # VCC/GND shapes      (green, same as wire)
# VCC/GND text uses C_REF (teal) — see _vcc_sym/_gnd_sym
FONT      = 'SF Mono, ui-monospace, Courier New, monospace'
FS_REF    = 11
FS_VAL    = 10
FS_LABEL  = 11

# ---------------------------------------------------------------------------
# Layout constants (px)
# ---------------------------------------------------------------------------
ROW_HEIGHT = 140    # vertical spacing between net rows
COL_WIDTH  = 180    # horizontal spacing between component columns
MARGIN_X   = 90     # horizontal margin
MARGIN_Y   = 80     # vertical margin
SYM_SCALE  = 10.0   # px per mm

# ---------------------------------------------------------------------------
# Power-net detection
# ---------------------------------------------------------------------------
_VCC_RE = re.compile(
    r'^(VCC|VDD|\+V|\+[0-9]|PWR|AVCC|DVCC|VBAT|VSUP|VBUS|V_[0-9]|\+3V|'
    r'\+5V|\+12V|3V3|5V|12V|VMAIN|VIN|VOUT_PWR)',
    re.IGNORECASE,
)
_GND_RE = re.compile(
    r'^(GND|VSS|AGND|DGND|PGND|EARTH|0V|GND_|SGND|-V|-[0-9])',
    re.IGNORECASE,
)


def _is_vcc(n: str) -> bool:
    return bool(_VCC_RE.match(n))


def _is_gnd(n: str) -> bool:
    return bool(_GND_RE.match(n))


def _is_power(n: str) -> bool:
    return _is_vcc(n) or _is_gnd(n)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class _Net:
    def __init__(self, name: str) -> None:
        self.name      = name
        self.level     = -1      # BFS level (set during layout)
        self.y         = 0.0     # SVG y (set during layout)
        self.display   = name    # overridable label text
        self.has_label = False   # True when explicitly named via `label` keyword


class _Comp:
    def __init__(self, ref: str, lib: str, sym: str, value: str) -> None:
        self.ref      = ref
        self.lib      = lib
        self.sym      = sym
        self.value    = value
        self.nets: Dict[int, str] = {}   # pin_num (1-based int) → net_name
        # Placement (filled during layout):
        self.cx       = 0.0
        self.cy       = 0.0
        self.rotation = 0.0
        self.col      = 0.0    # fractional column index
        self.from_lv  = 0
        self.to_lv    = 0
        self.anchor_ref = ""   # optional component reference used for clustering

# ---------------------------------------------------------------------------
# Netlist parser
# ---------------------------------------------------------------------------

def _parse(text: str) -> Tuple[Dict[str, _Comp], Dict[str, _Net]]:
    comps: Dict[str, _Comp] = {}
    nets:  Dict[str, _Net]  = {}

    def _net(name: str) -> _Net:
        if name not in nets:
            nets[name] = _Net(name)
        return nets[name]

    for raw in text.split("\n"):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        if parts[0].lower() == "label":
            # label  net_name  display_text
            if len(parts) >= 3:
                n = _net(parts[1])
                n.display   = " ".join(parts[2:]).strip('"')
                n.has_label = True
            continue

        # Ref  Lib:Symbol  Value  net1  net2  ...
        if len(parts) < 5:
            continue

        ref, lib_sym, value = parts[0], parts[1], parts[2]
        pin_nets = parts[3:]

        if ":" not in lib_sym:
            lib_sym = "Device:" + lib_sym
        lib, sym = lib_sym.split(":", 1)

        comp = _Comp(ref, lib, sym, value)
        for i, nn in enumerate(pin_nets, 1):
            comp.nets[i] = nn
            _net(nn)

        comps[ref] = comp

    return comps, nets

# ---------------------------------------------------------------------------
# Net-level assignment (BFS from VCC → assign integer depth to every net)
# ---------------------------------------------------------------------------

def _assign_levels(comps: Dict[str, _Comp], nets: Dict[str, _Net]) -> None:
    levels: Dict[str, int] = {}

    # Seed VCC-type nets at level 0
    for name in nets:
        if _is_vcc(name):
            levels[name] = 0

    if not levels:
        # No power nets — seed from the net that appears first in the netlist
        first = next(iter(nets))
        levels[first] = 0

    # Iterative BFS propagation through components
    changed = True
    while changed:
        changed = False
        for comp in comps.values():
            nn = list(comp.nets.values())
            known = {n: levels[n] for n in nn if n in levels}
            if not known:
                continue
            min_lv = min(known.values())
            for n in nn:
                if n not in levels or levels[n] > min_lv + 1:
                    levels[n] = min_lv + 1
                    changed = True

    # Force GND-type nets to the maximum computed level
    if any(_is_gnd(n) for n in nets):
        max_lv = max(levels.values()) if levels else 1
        for name in nets:
            if _is_gnd(name) and levels.get(name, 0) < max_lv:
                levels[name] = max_lv

    # Assign any remaining unvisited nets to the middle
    max_lv = max(levels.values()) if levels else 1
    for name in nets:
        if name not in levels:
            levels[name] = max_lv // 2

    for name, net in nets.items():
        net.level = levels.get(name, 0)

# ---------------------------------------------------------------------------
# Auto-rotation: orient each component so pin1 is at the top (near VCC)
# ---------------------------------------------------------------------------

def _best_rotation(comp: _Comp, nets: Dict[str, _Net]) -> float:
    """Return the rotation (0/90/180/270) that places the lowest-level net
    pin at the top of the component (lowest SVG y)."""
    if len(comp.nets) < 2:
        return 0.0

    # Find which pin connects to the lower-level net (closer to VCC)
    pin_lv = {pnum: nets[nn].level for pnum, nn in comp.nets.items() if nn in nets}
    if not pin_lv:
        return 0.0

    top_pnum = min(pin_lv, key=pin_lv.get)  # pin that should go to the top
    top_key  = str(int(top_pnum))

    # Try each rotation; pick the first that puts top_pnum at minimum SVG y
    for rot in (0, 270, 90, 180):
        pins = get_pin_positions(comp.lib, comp.sym, 0, 0, rot, SYM_SCALE)
        if top_key not in pins:
            continue
        top_y  = pins[top_key][1]
        other_y = [v[1] for k, v in pins.items() if k != top_key]
        if not other_y:
            return float(rot)
        if top_y <= min(other_y) + 0.5:   # +0.5 tolerance for float rounding
            return float(rot)

    return 0.0

# ---------------------------------------------------------------------------
# Column assignment: components sharing the same (from_lv, to_lv) span go
# into adjacent columns, centred around 0.
# ---------------------------------------------------------------------------

def _assign_columns(comps: Dict[str, _Comp], nets: Dict[str, _Net]) -> None:
    span_groups: Dict[Tuple[int, int], List[str]] = {}
    for ref, comp in comps.items():
        lvs = sorted({nets[nn].level for nn in comp.nets.values() if nn in nets})
        span = (lvs[0], lvs[-1]) if len(lvs) >= 2 else (lvs[0], lvs[0])
        span_groups.setdefault(span, []).append(ref)

    for refs in span_groups.values():
        n = len(refs)
        for i, ref in enumerate(refs):
            comps[ref].col = i - (n - 1) / 2.0   # centred around 0


def _is_capacitor(comp: _Comp) -> bool:
    return comp.sym.strip().upper().startswith("C")


def _is_decoupling_cap(comp: _Comp, nets: Dict[str, _Net]) -> bool:
    """Return True for a classic decoupling capacitor (power + ground only)."""
    if not _is_capacitor(comp):
        return False
    if len(comp.nets) != 2:
        return False
    net_names = list(comp.nets.values())
    has_power = any(_is_power(nn) for nn in net_names)
    has_ground = any(_is_gnd(nn) for nn in net_names)
    return has_power and has_ground


def _component_score(comp: _Comp, nets: Dict[str, _Net]) -> Tuple[int, int, int, int, str]:
    """Score a component for use as a power-cluster anchor.

    Higher is better. We prefer large, non-passive parts with many non-power pins.
    """
    pin_count = len(comp.nets)
    non_power_pins = sum(1 for nn in comp.nets.values() if not _is_power(nn))
    ref_prefix = comp.ref[:1].upper()
    active_bonus = 1 if ref_prefix in {"U", "A", "X", "M", "Q"} else 0
    connector_penalty = 1 if ref_prefix in {"J", "P", "K", "T"} else 0
    passive_penalty = 1 if _is_capacitor(comp) or comp.sym.strip().upper().startswith(("R", "L", "D")) else 0
    return (active_bonus, pin_count, non_power_pins, -(passive_penalty + connector_penalty), comp.ref)


def _stagger_offsets(count: int, step: float = 0.42) -> List[float]:
    """Return small symmetric column offsets for clustered parts."""
    if count <= 0:
        return []
    if count == 1:
        return [step]

    offsets: List[float] = []
    pair = 1
    while len(offsets) < count:
        offsets.append(pair * step)
        if len(offsets) >= count:
            break
        offsets.append(-pair * step)
        pair += 1
    return offsets


def _apply_power_cluster_layout(comps: Dict[str, _Comp], nets: Dict[str, _Net]) -> None:
    """Cluster decoupling capacitors near the IC that consumes the rail.

    The generic span-based layout is good for signal chains, but it leaves
    decoupling capacitors looking detached from the load they stabilize.
    We detect power-to-ground capacitors and pull them into the same column
    family as the strongest non-passive consumer on that power net.
    """
    power_nets = [name for name in nets if _is_power(name)]
    if not power_nets:
        return

    # Build a basic anchor per power net.
    anchor_by_net: Dict[str, str] = {}
    for net_name in power_nets:
        candidates: List[Tuple[Tuple[int, int, int, str], str]] = []
        for comp in comps.values():
            if net_name not in comp.nets.values():
                continue
            if _is_decoupling_cap(comp, nets):
                continue
            candidates.append((_component_score(comp, nets), comp.ref))
        if candidates:
            candidates.sort(reverse=True)
            anchor_by_net[net_name] = candidates[0][1]

    # Group decoupling capacitors by power net and place them beside the anchor.
    caps_by_net: Dict[str, List[str]] = {}
    for ref, comp in comps.items():
        if not _is_decoupling_cap(comp, nets):
            continue
        for net_name in comp.nets.values():
            if _is_vcc(net_name):
                caps_by_net.setdefault(net_name, []).append(ref)
                comp.anchor_ref = anchor_by_net.get(net_name, "")
                break

    for net_name, refs in caps_by_net.items():
        anchor_ref = anchor_by_net.get(net_name, "")
        if not anchor_ref or anchor_ref not in comps:
            continue
        anchor_col = comps[anchor_ref].col
        offsets = _stagger_offsets(len(refs))
        for ref, offset in zip(sorted(refs), offsets):
            comps[ref].col = anchor_col + offset
            comps[ref].anchor_ref = anchor_ref

# ---------------------------------------------------------------------------
# Full layout: assign (cx, cy, rotation) to every component and y to every net
# ---------------------------------------------------------------------------

_WIRE_MARGIN = 30   # min clearance (px) between a bus line and the component body edge
_PIN_SP_LAYOUT = 26  # must match sym_renderer._PIN_SPACING


def _comp_body_height(comp: '_Comp') -> float:
    """Estimate the SVG height (y-span) of a component at its assigned rotation."""
    import math as _math
    # Custom symbol → exact IC box formula
    custom = load_custom_symbol(comp.sym)
    if custom and custom.get("pins"):
        left_n = _math.ceil(len(custom["pins"]) / 2)
        return float((left_n + 1) * _PIN_SP_LAYOUT)
    # KiCad library: use actual pin y-span
    pins = get_pin_positions(comp.lib, comp.sym, 0, 0, comp.rotation, SYM_SCALE)
    if pins and len(pins) >= 2:
        ys = [v[1] for v in pins.values()]
        return float(max(ys) - min(ys))
    # Generic IC fallback
    n = max(comp.nets.keys()) if comp.nets else 2
    left_n = _math.ceil(n / 2)
    return float((left_n + 1) * _PIN_SP_LAYOUT)


def _layout(comps: Dict[str, _Comp], nets: Dict[str, _Net]) -> Tuple[float, float]:
    """Assign positions and return (canvas_width, canvas_height).

    Level-gap heights are computed dynamically so that every component body
    fits entirely between the two bus lines that bracket it.
    """
    _assign_levels(comps, nets)

    for comp in comps.values():
        comp.rotation = _best_rotation(comp, nets)

    _assign_columns(comps, nets)
    _apply_power_cluster_layout(comps, nets)

    max_lv  = max((n.level for n in nets.values()), default=1)
    max_col = max((abs(c.col) for c in comps.values()), default=0)
    n_cols  = int(max_col * 2) + 1

    canvas_w = MARGIN_X * 2 + max(1, n_cols) * COL_WIDTH + 120

    # ── Per-level-gap height ────────────────────────────────────────────
    # gap_h[i] = height in px between level i and level i+1.
    # Minimum = ROW_HEIGHT; bumped up when a component spanning that gap
    # is taller than (gap * ROW_HEIGHT - 2*_WIRE_MARGIN).
    gap_h: Dict[int, float] = {i: float(ROW_HEIGHT) for i in range(max_lv)}

    for comp in comps.values():
        lvs = sorted({nets[nn].level for nn in comp.nets.values() if nn in nets})
        if len(lvs) < 2:
            continue
        lo, hi = lvs[0], lvs[-1]
        span = hi - lo
        body_h = _comp_body_height(comp)
        needed_per_gap = (body_h + 2.0 * _WIRE_MARGIN) / span
        for i in range(lo, hi):
            if needed_per_gap > gap_h.get(i, ROW_HEIGHT):
                gap_h[i] = needed_per_gap

    # Cumulative y for each level
    level_y: Dict[int, float] = {0: float(MARGIN_Y)}
    for i in range(1, max_lv + 1):
        level_y[i] = level_y[i - 1] + gap_h.get(i - 1, ROW_HEIGHT)

    canvas_h = level_y[max_lv] + MARGIN_Y

    # Net y positions
    for net in nets.values():
        net.y = level_y.get(net.level, MARGIN_Y + net.level * ROW_HEIGHT)

    # Component centres
    cx_base = canvas_w / 2
    for comp in comps.values():
        lvs = sorted({nets[nn].level for nn in comp.nets.values() if nn in nets})
        comp.from_lv = lvs[0] if lvs else 0
        comp.to_lv   = lvs[-1] if lvs else 0
        comp.cy = (level_y[comp.from_lv] + level_y[comp.to_lv]) / 2.0
        comp.cx = cx_base + comp.col * COL_WIDTH

    return canvas_w, canvas_h

# ---------------------------------------------------------------------------
# SVG primitives
# ---------------------------------------------------------------------------

def _wire(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        '<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
        'stroke="{}" stroke-width="1.5" stroke-linecap="round"/>'.format(
            x1, y1, x2, y2, C_WIRE)
    )


def _junction(x: float, y: float) -> str:
    return '<circle cx="{:.1f}" cy="{:.1f}" r="3" fill="{}"/>'.format(x, y, C_JUNC)


def _vcc_sym(x: float, y: float, label: str = "VCC") -> str:
    return (
        '<line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{ty:.1f}" '
        'stroke="{c}" stroke-width="1.5"/>'
        '<polygon points="{x1:.1f},{ty:.1f} {x2:.1f},{ty:.1f} {x:.1f},{pt:.1f}" fill="{c}"/>'
        '<text x="{tx:.1f}" y="{tty:.1f}" fill="{ct}" '
        'font-size="{fs}" font-family="{ff}">{lbl}</text>'
    ).format(
        x=x, y=y, ty=y - 22,
        x1=x - 9, x2=x + 9, pt=y - 35,
        tx=x + 13, tty=y - 18,
        c=C_PWR, ct=C_REF, fs=FS_LABEL, ff=FONT,
        lbl=_html.escape(label),
    )


def _gnd_sym(x: float, y: float, label: str = "GND") -> str:
    return (
        '<line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{y2:.1f}" '
        'stroke="{c}" stroke-width="1.5"/>'
        '<line x1="{a1:.1f}" y1="{y2:.1f}" x2="{a2:.1f}" y2="{y2:.1f}" '
        'stroke="{c}" stroke-width="2.0"/>'
        '<line x1="{b1:.1f}" y1="{y3:.1f}" x2="{b2:.1f}" y2="{y3:.1f}" '
        'stroke="{c}" stroke-width="1.5"/>'
        '<line x1="{d1:.1f}" y1="{y4:.1f}" x2="{d2:.1f}" y2="{y4:.1f}" '
        'stroke="{c}" stroke-width="1.0"/>'
        '<text x="{tx:.1f}" y="{ty:.1f}" fill="{ct}" '
        'font-size="{fs}" font-family="{ff}">{lbl}</text>'
    ).format(
        x=x, y=y, y2=y + 14, y3=y + 21, y4=y + 28,
        a1=x - 14, a2=x + 14,
        b1=x - 9, b2=x + 9,
        d1=x - 4, d2=x + 4,
        tx=x + 17, ty=y + 20,
        c=C_PWR, ct=C_REF, fs=FS_LABEL, ff=FONT,
        lbl=_html.escape(label),
    )


def _comp_labels(cx: float, cy: float, ref: str, value: str) -> str:
    return (
        '<text x="{:.1f}" y="{:.1f}" fill="{}" font-size="{}" '
        'font-family="{}" text-anchor="start">{}</text>'
        '<text x="{:.1f}" y="{:.1f}" fill="{}" font-size="{}" '
        'font-family="{}" text-anchor="start">{}</text>'
    ).format(
        cx + 17, cy - 4, C_REF, FS_REF, FONT, _html.escape(ref),
        cx + 17, cy + 12, C_VAL, FS_VAL, FONT, _html.escape(value),
    )


def _net_label(x: float, y: float, text: str, anchor: str = "start") -> str:
    return (
        '<text x="{:.1f}" y="{:.1f}" fill="{}" font-size="{}" '
        'font-family="{}" text-anchor="{}" dominant-baseline="middle">{}</text>'
    ).format(x, y, C_LABEL, FS_LABEL, FONT, anchor, _html.escape(text))


def _grid(w: float, h: float) -> str:
    return (
        '<defs>'
        '<pattern id="kgrid" x="0" y="0" width="10" height="10" patternUnits="userSpaceOnUse">'
        '<path d="M 10 0 L 0 0 0 10" fill="none" stroke="{c}" stroke-width="0.4"/>'
        '</pattern>'
        '</defs>'
        '<rect width="{w:.0f}" height="{h:.0f}" fill="url(#kgrid)"/>'
    ).format(c=C_GRID, w=w, h=h)

# ---------------------------------------------------------------------------
# Wire routing
# ---------------------------------------------------------------------------

def _pin_pos(comp: _Comp, pin_num: int) -> Optional[Tuple[float, float]]:
    """Return the SVG (x, y) of a specific pin in placed coordinates.
    Priority: 1) custom JSON cache (matches render priority), 2) KiCad lib, 3) generic IC."""
    # Custom cache takes priority — must match the render_custom_ic layout
    custom = load_custom_symbol(comp.sym)
    if custom and custom.get("pins"):
        pins = get_custom_pin_positions(custom["pins"], comp.cx, comp.cy)
        return pins.get(str(pin_num))
    # KiCad library symbol (with fuzzy match)
    pins = get_pin_positions(comp.lib, comp.sym, comp.cx, comp.cy, comp.rotation, SYM_SCALE)
    if pins:
        key = str(pin_num)
        return pins.get(key)
    # Generic IC fallback
    n = max(comp.nets.keys()) if comp.nets else 2
    pins = get_generic_pin_positions(n, comp.cx, comp.cy)
    return pins.get(str(pin_num))


def _route(comps: Dict[str, _Comp], nets: Dict[str, _Net]) -> List[str]:
    """
    Generate all wire + junction SVG elements for every net.

    Rules:
    • Single-endpoint signal net  → no routing, just a net label at the pin tip
      (the stub is already drawn as part of the component symbol).
    • Multi-endpoint net          → vertical segment from each pin to bus_y,
      then horizontal bus only when there are ≥2 distinct x positions.
    • Power nets (VCC/GND)        → vertical stub from pin to bus_y + power symbol.
    • No wire shares a net bus with any other net.
    • For horizontal IC pins, the routing adds a 10-px clearance extension
      before the vertical segment (guarantees no 90° bend at the pin tip).
    """
    svgs: List[str] = []

    for net_name, net in nets.items():
        # ── Collect all pin positions for this net ─────────────────────
        pin_pts: List[Tuple[float, float, str, int]] = []   # x, y, comp_ref, pin_num
        for comp in comps.values():
            for pnum, nn in comp.nets.items():
                if nn != net_name:
                    continue
                pos = _pin_pos(comp, pnum)
                if pos is not None:
                    pin_pts.append((pos[0], pos[1], comp.ref, pnum))

        if not pin_pts:
            continue

        is_power = _is_vcc(net_name) or _is_gnd(net_name)
        bus_y    = net.y

        # ── Single-endpoint non-power net: label at pin, no routing ────
        if len(pin_pts) == 1 and not is_power:
            if net.has_label or net.display != net_name:
                px, py = pin_pts[0][0], pin_pts[0][1]
                ref    = pin_pts[0][2]
                comp_cx = comps[ref].cx if ref in comps else px
                # Place label on the open (stub-tip) side
                if px < comp_cx - 5:          # left-side IC pin
                    svgs.append(_net_label(px - 4, py, net.display, "end"))
                elif px > comp_cx + 5:         # right-side IC pin
                    svgs.append(_net_label(px + 4, py, net.display, "start"))
                else:                          # vertical passive pin
                    svgs.append(_net_label(px + 4, py, net.display, "start"))
            continue

        # ── Collect routing points (with clearance extension for IC pins) ─
        # route_pts: the x,y where the *vertical* wire segment starts.
        # For horizontal IC pins we extend 10 px in the stub direction first.
        route_pts: List[Tuple[float, float]] = []
        for px, py, ref, _ in pin_pts:
            comp_cx = comps[ref].cx if ref in comps else px
            if px < comp_cx - 5:              # left-side IC pin → extend left
                ext_x = px - 10
                svgs.append(_wire(px, py, ext_x, py))
                route_pts.append((ext_x, py))
            elif px > comp_cx + 5:             # right-side IC pin → extend right
                ext_x = px + 10
                svgs.append(_wire(px, py, ext_x, py))
                route_pts.append((ext_x, py))
            else:
                route_pts.append((px, py))     # passive/vertical: no extension

        # ── Vertical segments to bus_y ─────────────────────────────────
        xs = [round(p[0], 1) for p in route_pts]
        for (rx, ry) in route_pts:
            if abs(ry - bus_y) > 1:
                svgs.append(_wire(rx, ry, rx, bus_y))

        # ── Horizontal bus (signal nets only, ≥2 distinct x positions) ─
        distinct_xs = sorted(set(xs))
        multi_col   = len(distinct_xs) > 1
        if multi_col and not is_power:
            svgs.append(_wire(distinct_xs[0], bus_y, distinct_xs[-1], bus_y))

        # ── Power symbols: one independent symbol per pin, NO shared bus ─
        # (Power nets are global — no horizontal wire needed between symbols)
        if _is_vcc(net_name):
            for rx, ry in route_pts:
                svgs.append(_vcc_sym(rx, bus_y, net.display))
        elif _is_gnd(net_name):
            for rx, ry in route_pts:
                svgs.append(_gnd_sym(rx, bus_y, net.display))

        else:
            # ── Net labels for multi-endpoint named/branching nets ──────
            if net.has_label or multi_col or net.display != net_name:
                label_x = distinct_xs[-1] + 16
                svgs.append(_net_label(label_x, bus_y, net.display, "start"))

            # ── Junction dots (LAST: on top of symbols, wires, labels) ──
            if multi_col:
                for rx in distinct_xs:
                    rpts = [(rx2, ry2) for rx2, ry2 in route_pts if abs(rx2 - rx) < 1]
                    has_above = any(ry2 < bus_y - 1 for _, ry2 in rpts)
                    has_below = any(ry2 > bus_y + 1 for _, ry2 in rpts)
                    has_left  = rx > distinct_xs[0] + 1
                    has_right = rx < distinct_xs[-1] - 1
                    if int(has_above) + int(has_below) + int(has_left) + int(has_right) >= 3:
                        svgs.append(_junction(rx, bus_y))

    return svgs

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_netlist(text: str) -> str:
    """
    Parse a netlist block and return a complete, self-contained SVG string
    using real KiCad symbols, automatic placement, and KiCad dark-mode theme.
    """
    comps, nets = _parse(text)

    if not comps:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="280" height="60">'
            '<rect width="100%" height="100%" fill="{}"/>'
            '<text x="14" y="34" fill="{}" font-size="12" font-family="{}">'
            'No components parsed.</text></svg>'
        ).format(C_BG, C_VAL, FONT)

    canvas_w, canvas_h = _layout(comps, nets)

    parts: List[str] = []

    # Background
    parts.append('<rect width="100%" height="100%" fill="{}"/>'.format(C_BG))

    # 1 mm grid overlay
    parts.append(_grid(canvas_w, canvas_h))

    # ── Layer 1: wires, power symbols, net labels (drawn first / behind) ──
    parts.extend(_route(comps, nets))

    # ── Layer 2: component symbols + ref/value labels (drawn on top) ──────
    for comp in comps.values():
        # Priority: 1) custom JSON cache (compact box with named pins)
        #           2) KiCad library symbol
        #           3) generic numbered IC box
        custom = load_custom_symbol(comp.sym)
        is_ic_box = False
        if custom and custom.get("pins"):
            g = render_custom_ic(comp.sym, custom["pins"], comp.cx, comp.cy)
            is_ic_box = True
            n_pins = len(custom["pins"])
        else:
            g = render_symbol_group(comp.lib, comp.sym, comp.cx, comp.cy, comp.rotation, SYM_SCALE)
            if not g or g.strip() in ("", "<g>\n  \n</g>", "<g></g>"):
                # Fall back to anonymous generic IC box
                n_pins = max(comp.nets.keys()) if comp.nets else 2
                g = render_generic_ic(comp.sym, n_pins, comp.cx, comp.cy)
                is_ic_box = True
        parts.append(g)
        if is_ic_box:
            # Ref/value below the IC box (avoid overlapping body + pin labels)
            import math
            left_n = math.ceil(n_pins / 2)
            _PIN_SP = 26   # must match sym_renderer._PIN_SPACING
            box_h   = (left_n + 1) * _PIN_SP
            box_bot = comp.cy + box_h / 2 + 6
            parts.append(
                '<text x="{:.1f}" y="{:.1f}" fill="{}" font-size="{}" '
                'font-family="{}" text-anchor="middle">{}</text>'.format(
                    comp.cx, box_bot, C_REF, FS_REF, FONT, _html.escape(comp.ref))
            )
            parts.append(
                '<text x="{:.1f}" y="{:.1f}" fill="{}" font-size="{}" '
                'font-family="{}" text-anchor="middle">{}</text>'.format(
                    comp.cx, box_bot + 12, C_VAL, FS_VAL, FONT, _html.escape(comp.value))
            )
        else:
            parts.append(_comp_labels(comp.cx, comp.cy, comp.ref, comp.value))

    body = "\n  ".join(parts)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="{w:.0f}" height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}">\n'
        '  {body}\n'
        '</svg>'
    ).format(w=canvas_w, h=canvas_h, body=body)


# ---------------------------------------------------------------------------
# Schemdraw backend
# ---------------------------------------------------------------------------

_LEGACY_RENDER_NETLIST = render_netlist
_schemdraw = None
_sd = None


def _load_schemdraw() -> bool:
    """Import Schemdraw lazily so the plugin startup stays fast."""
    global _schemdraw, _sd
    if _schemdraw is not None and _sd is not None:
        return True
    try:
        import schemdraw as _schemdraw_mod  # type: ignore
        import schemdraw.elements as _sd_mod  # type: ignore
    except Exception:
        return False
    _schemdraw = _schemdraw_mod
    _sd = _sd_mod
    return True


def _sd_point(x: float, y: float, scale: float) -> Tuple[float, float]:
    return (x / scale, y / scale)


def _sd_pin_side(comp: _Comp, pin_num: int, total_pins: int) -> str:
    net_name = comp.nets.get(pin_num, "")
    if _is_vcc(net_name):
        return "T"
    if _is_gnd(net_name):
        return "B"

    pin_label = net_name.upper()
    if any(tok in pin_label for tok in ("IN", "RX", "SCL", "SDA", "CLK", "CS", "NSS", "RST", "EN", "ADC", "DIN")):
        return "L"
    if any(tok in pin_label for tok in ("OUT", "TX", "MISO", "MOSI", "BUSY", "DIO", "IRQ", "PWM", "AOUT", "VOUT")):
        return "R"

    return "L" if pin_num <= max(1, total_pins // 2) else "R"


def _sd_component_label(comp: _Comp) -> str:
    value = (comp.value or "").strip()
    return f"{comp.ref}\n{value}" if value else comp.ref


def _sd_primitive_for_two_terminal(comp: _Comp):
    sym = (comp.sym or "").upper()
    if sym.startswith("R"):
        return _sd.Resistor
    if sym.startswith("L"):
        return _sd.Inductor
    if sym.startswith("D") or sym.startswith("LED") or sym.startswith("ZENER"):
        return _sd.Diode
    return _sd.Capacitor


def _sd_make_ic(comp: _Comp) -> Any:
    custom = load_custom_symbol(comp.sym)
    pin_names: Dict[str, str] = {}
    if custom and isinstance(custom.get("pins"), dict):
        pin_names = {str(k): str(v) for k, v in custom["pins"].items()}
    else:
        for pnum, net_name in comp.nets.items():
            pin_names[str(pnum)] = net_name

    pins = []
    for pnum in sorted(comp.nets.keys()):
        side = _sd_pin_side(comp, pnum, len(comp.nets))
        pins.append(_sd.IcPin(
            name=None,
            pin=str(pnum),
            side=side,
            anchorname=f"pin{pnum}",
            lblsize=8,
            pinlblsize=8,
        ))

    pin_count = len(pins)
    height = 1.6 + max(0, pin_count - 4) * 0.22
    width = 3.2
    return _sd.Ic(pins=pins, size=(width, height), pinspacing=0.35)


def _sd_component_bbox_center(elem: Any) -> Tuple[float, float]:
    bbox = elem.get_bbox()
    return ((bbox.xmin + bbox.xmax) / 2.0, (bbox.ymin + bbox.ymax) / 2.0)


def _sd_add_label(drawing: Any, text: str, x: float, y: float) -> None:
    drawing += _sd.Label(text).at((x, y))


def _sd_render_netlist(text: str) -> str:
    if not _load_schemdraw():
        return _LEGACY_RENDER_NETLIST(text)

    comps, nets = _parse(text)
    if not comps:
        return _LEGACY_RENDER_NETLIST(text)

    _layout(comps, nets)

    # Use px-like coordinates directly, but keep the drawing scale gentle.
    _schemdraw.config(
        unit=1.0,
        inches_per_unit=0.010,
        lblofst=0.10,
        fontsize=10.0,
        font="monospace",
        color=C_BODY,
        lw=1.6,
        bgcolor=C_BG,
        margin=0.18,
    )
    drawing = _schemdraw.Drawing(show=False)

    scale = 1.0
    pin_positions: Dict[Tuple[str, int], Tuple[float, float]] = {}

    # First pass: draw components and capture pin positions.
    for comp in comps.values():
        if len(comp.nets) == 2:
            pin1 = _pin_pos(comp, 1)
            pin2 = _pin_pos(comp, 2)
            if pin1 is None or pin2 is None:
                continue
            start = _sd_point(pin1[0], pin1[1], scale)
            end = _sd_point(pin2[0], pin2[1], scale)
            ctor = _sd_primitive_for_two_terminal(comp)
            if ctor is _sd.Diode and (comp.sym.upper().startswith("LED") or comp.sym.upper().startswith("ZENER")):
                elem = ctor()
            else:
                elem = ctor()
            elem.endpoints(start, end)
            drawing += elem
            pin_positions[(comp.ref, 1)] = tuple(getattr(elem, "start"))
            pin_positions[(comp.ref, 2)] = tuple(getattr(elem, "end"))

            # Reference and value labels above/below the symbol center.
            cx, cy = _sd_component_bbox_center(elem)
            _sd_add_label(drawing, comp.ref, cx, cy - 0.55)
            if comp.value.strip():
                _sd_add_label(drawing, comp.value.strip(), cx, cy + 0.55)
            continue

        elem = _sd_make_ic(comp)
        elem.anchor("center").at(_sd_point(comp.cx, comp.cy, scale))
        drawing += elem
        for pnum in sorted(comp.nets.keys()):
            anchor_name = f"pin{pnum}"
            try:
                pin_positions[(comp.ref, pnum)] = tuple(getattr(elem, anchor_name))
            except Exception:
                continue

        cx, cy = _sd_component_bbox_center(elem)
        _sd_add_label(drawing, comp.ref, cx, cy + 0.85)
        if comp.value.strip():
            _sd_add_label(drawing, comp.value.strip(), cx, cy + 1.15)

    # Second pass: route nets. Use the same row structure as the legacy layout.
    for net_name, net in nets.items():
        endpoints: List[Tuple[str, int, float, float]] = []
        for comp in comps.values():
            for pnum, nn in comp.nets.items():
                if nn != net_name:
                    continue
                pt = pin_positions.get((comp.ref, pnum))
                if pt is None:
                    pos = _pin_pos(comp, pnum)
                    if pos is None:
                        continue
                    pt = _sd_point(pos[0], pos[1], scale)
                endpoints.append((comp.ref, pnum, pt[0], pt[1]))

        if not endpoints:
            continue

        bus_y = net.y / scale
        is_power = _is_power(net_name)

        # Single endpoint non-power nets are left as labels rather than a long stub.
        if len(endpoints) == 1 and not is_power:
            _sd_add_label(drawing, net.display, endpoints[0][2] + 0.4, endpoints[0][3])
            continue

        route_pts: List[Tuple[float, float]] = []
        for _ref, _pnum, px, py in endpoints:
            route_pts.append((px, py))

        xs = sorted({round(px, 2) for px, _ in route_pts})
        for px, py in route_pts:
            if abs(py - bus_y) > 1e-3:
                drawing += _sd.Line().endpoints((px, py), (px, bus_y))

        if len(xs) > 1:
            drawing += _sd.Line().endpoints((xs[0], bus_y), (xs[-1], bus_y))

        if is_power:
            for px in xs:
                if _is_vcc(net_name):
                    drawing += _sd.Vdd().at((px, bus_y))
                else:
                    drawing += _sd.Vss().at((px, bus_y))
        else:
            if net.has_label or net.display != net_name or len(xs) > 1:
                _sd_add_label(drawing, net.display, xs[-1] + 0.4, bus_y)
            for px in xs:
                if len(xs) > 1:
                    drawing += _sd.Dot().at((px, bus_y))

    svg_path = None
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as tmp:
            svg_path = tmp.name
        drawing.save(svg_path)
        with open(svg_path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return _LEGACY_RENDER_NETLIST(text)
    finally:
        if svg_path and os.path.exists(svg_path):
            try:
                os.unlink(svg_path)
            except Exception:
                pass


# Keep the legacy renderer as the default for now.
# The Schemdraw backend remains available as `_sd_render_netlist` for
# experimentation, but the automatic placement still needs more work before it
# is good enough to replace the current output.
render_netlist = _LEGACY_RENDER_NETLIST
