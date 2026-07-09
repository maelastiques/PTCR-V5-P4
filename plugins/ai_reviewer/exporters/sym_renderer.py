"""
Parse KiCad .kicad_sym symbol library entries and render them to SVG.

Coordinate system
-----------------
KiCad .kicad_sym uses **mm** with Y+ pointing **UP** (math convention).
SVG uses **px** with Y+ pointing **DOWN** (screen convention).

Transform applied when placing a symbol at SVG centre (cx, cy) with scale s:
    svg_x = kicad_x * s + cx
    svg_y = -kicad_y * s + cy

Default scale: 10 px/mm.

Public API
----------
render_symbol_group(lib, sym, cx, cy, rotation=0, scale=10.0)
    -> SVG <g> element string (component body + pin lines).

get_pin_positions(lib, sym, cx, cy, rotation=0, scale=10.0)
    -> dict  {pin_number_str: (svg_x, svg_y)}

render_circuit_dsl(dsl_text)
    -> complete standalone SVG string from a simple placement DSL.
"""

from __future__ import annotations
import html as _html_mod
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_LIBS_DIR = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"
# Plugin-local cache for AI-reconstructed custom symbols
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_DIR = os.path.join(_PLUGIN_DIR, "cache", "custom_symbols")

_COL_BODY      = "#840000"   # component outlines & pin lines  (dark red, KiCad light)
_COL_FILL      = "#ffffc2"   # component body fill              (light yellow, KiCad light)
_COL_TEXT_REF  = "#006464"   # reference designator (R1, C1 …) (teal)
_COL_TEXT_VAL  = "#006464"   # value text (10k, 100nF …)        (teal)
_COL_BG = "#1a1d22"
_DEFAULT_SW = 1.5       # default stroke-width (px)
_FONT = "monospace"
_FONT_SIZE = 11

_SYMBOL_ALIASES = {
    ("Device", "R"): ("Device", "R_US"),
    ("Device", "R_Small"): ("Device", "R_Small_US"),
    ("Device", "C"): ("Device", "C_Small"),
    ("Device", "NMOS"): ("Transistor_FET", "Q_NMOS_GDS"),
    ("Device", "PMOS"): ("Transistor_FET", "Q_PMOS_GDS"),
    ("Simulation_SPICE", "NMOS"): ("Transistor_FET", "Q_NMOS_GDS"),
    ("Simulation_SPICE", "PMOS"): ("Transistor_FET", "Q_PMOS_GDS"),
}

# ---------------------------------------------------------------------------
# S-expression tokenizer / parser
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\n\r":
            i += 1
        elif c == "(":
            tokens.append("("); i += 1
        elif c == ")":
            tokens.append(")"); i += 1
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\": j += 2
                elif text[j] == '"': break
                else: j += 1
            tokens.append(text[i + 1 : j]); i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\n\r()": j += 1
            tokens.append(text[i:j]); i = j
    return tokens


def _parse_sexp(tokens: List[str], pos: int) -> Tuple[Any, int]:
    if pos >= len(tokens):
        return None, pos
    if tokens[pos] != "(":
        tok = tokens[pos]
        try:
            return float(tok), pos + 1
        except ValueError:
            return tok, pos + 1
    result: List[Any] = []
    pos += 1
    while pos < len(tokens) and tokens[pos] != ")":
        item, pos = _parse_sexp(tokens, pos)
        result.append(item)
    return result, pos + 1


def _find(node: list, key: str) -> Optional[list]:
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def _findall(node: list, key: str) -> List[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == key]

# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def _tx(kx: float, s: float, cx: float) -> float:
    return kx * s + cx


def _ty(ky: float, s: float, cy: float) -> float:
    return -ky * s + cy


def _fmt(v: float) -> str:
    return "{:.2f}".format(v)


def _rot(kx: float, ky: float, deg: float) -> Tuple[float, float]:
    """Rotate a KiCad-space point (kx, ky) by deg degrees CCW around the origin."""
    if deg == 0:
        return kx, ky
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return kx * c - ky * s, kx * s + ky * c

# ---------------------------------------------------------------------------
# Arc helper  (3-point arc → SVG path data)
# ---------------------------------------------------------------------------

def _three_point_arc(sx: float, sy: float, mx: float, my: float,
                     ex: float, ey: float) -> str:
    """Return SVG path data for the arc through three SVG-space points."""
    ax, ay = sx, sy
    bx, by = mx, my
    cx, cy = ex, ey
    D = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(D) < 1e-7:
        return "M {},{} L {},{}".format(_fmt(sx), _fmt(sy), _fmt(ex), _fmt(ey))
    ux = ((ax*ax+ay*ay)*(by-cy) + (bx*bx+by*by)*(cy-ay) + (cx*cx+cy*cy)*(ay-by)) / D
    uy = ((ax*ax+ay*ay)*(cx-bx) + (bx*bx+by*by)*(ax-cx) + (cx*cx+cy*cy)*(bx-ax)) / D
    r = math.hypot(ax - ux, ay - uy)
    a_s = math.atan2(sy - uy, sx - ux)
    a_m = math.atan2(my - uy, mx - ux)
    a_e = math.atan2(ey - uy, ex - ux)
    # CW angular distance from a_from to a_to in SVG space
    # (SVG sweep=1 = clockwise on screen = increasing θ in formula y=cy+r·sin(θ))
    def _cw(f: float, t: float) -> float:
        return (t - f) % (2 * math.pi)
    d_m = _cw(a_s, a_m)
    d_e = _cw(a_s, a_e)
    if d_m < d_e:                          # mid is on the CW arc
        sweep, large_arc = 1, (1 if d_e > math.pi else 0)
    else:                                  # mid is on the CCW arc
        sweep, large_arc = 0, (1 if (2 * math.pi - d_e) > math.pi else 0)
    return "M {},{} A {},{} 0 {} {} {},{}".format(
        _fmt(sx), _fmt(sy), _fmt(r), _fmt(r), large_arc, sweep, _fmt(ex), _fmt(ey)
    )

# ---------------------------------------------------------------------------
# Stroke / fill helpers
# ---------------------------------------------------------------------------

def _sw(node: list, scale: float) -> float:
    st = _find(node, "stroke")
    if st:
        w = _find(st, "width")
        if w and len(w) > 1:
            v = float(w[1]) * scale
            return max(v, _DEFAULT_SW) if v > 0 else _DEFAULT_SW
    return _DEFAULT_SW


def _fill(node: list) -> str:
    f = _find(node, "fill")
    if f:
        t = _find(f, "type")
        if t and len(t) > 1 and str(t[1]) == "background":
            return _COL_FILL
    return "none"

# ---------------------------------------------------------------------------
# Primitive renderers
# ---------------------------------------------------------------------------

def _prim_rectangle(node: list, s: float, cx: float, cy: float, rot: float) -> str:
    sn = _find(node, "start"); en = _find(node, "end")
    if not sn or not en:
        return ""
    corners_k = [
        (float(sn[1]), float(sn[2])),
        (float(en[1]), float(sn[2])),
        (float(en[1]), float(en[2])),
        (float(sn[1]), float(en[2])),
    ]
    corners_svg = []
    for kx, ky in corners_k:
        rkx, rky = _rot(kx, ky, rot)
        corners_svg.append((_tx(rkx, s, cx), _ty(rky, s, cy)))
    # After rotation, a rectangle may become a tilted quad — use <polygon>
    # Always fill with body colour (KiCad light: light yellow background).
    pts = " ".join("{},{}".format(_fmt(x), _fmt(y)) for x, y in corners_svg)
    return '<polygon points="{}" stroke="{}" stroke-width="{}" fill="{}"/>'.format(
        pts, _COL_BODY, _fmt(_sw(node, s)), _COL_FILL
    )


def _prim_polyline(node: list, s: float, cx: float, cy: float, rot: float) -> str:
    pts_n = _find(node, "pts")
    if not pts_n:
        return ""
    xys = _findall(pts_n, "xy")
    if not xys:
        return ""
    pts_svg = []
    for p in xys:
        rkx, rky = _rot(float(p[1]), float(p[2]), rot)
        pts_svg.append((_tx(rkx, s, cx), _ty(rky, s, cy)))
    sw_v = _sw(node, s)
    if len(pts_svg) == 2:
        (x1, y1), (x2, y2) = pts_svg
        return '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" stroke-width="{}" fill="{}"/>'.format(
            _fmt(x1), _fmt(y1), _fmt(x2), _fmt(y2), _COL_BODY, _fmt(sw_v), _fill(node)
        )
    closed = _fill(node) != "none"
    tag = "polygon" if closed else "polyline"
    pts_str = " ".join("{},{}".format(_fmt(x), _fmt(y)) for x, y in pts_svg)
    return '<{tag} points="{pts}" stroke="{c}" stroke-width="{sw}" fill="{f}"/>'.format(
        tag=tag, pts=pts_str, c=_COL_BODY, sw=_fmt(sw_v), f=_fill(node)
    )


def _prim_arc(node: list, s: float, cx: float, cy: float, rot: float) -> str:
    sn = _find(node, "start"); mn = _find(node, "mid"); en = _find(node, "end")
    if not sn or not mn or not en:
        return ""
    srx, sry = _rot(float(sn[1]), float(sn[2]), rot)
    mrx, mry = _rot(float(mn[1]), float(mn[2]), rot)
    erx, ery = _rot(float(en[1]), float(en[2]), rot)
    sx, sy = _tx(srx, s, cx), _ty(sry, s, cy)
    mx, my = _tx(mrx, s, cx), _ty(mry, s, cy)
    ex, ey = _tx(erx, s, cx), _ty(ery, s, cy)
    d = _three_point_arc(sx, sy, mx, my, ex, ey)
    return '<path d="{}" stroke="{}" stroke-width="{}" fill="none"/>'.format(
        d, _COL_BODY, _fmt(_sw(node, s))
    )


def _prim_circle(node: list, s: float, cx: float, cy: float, rot: float) -> str:
    cn = _find(node, "center"); rn = _find(node, "radius")
    if not cn or not rn:
        return ""
    rkx, rky = _rot(float(cn[1]), float(cn[2]), rot)
    svx = _tx(rkx, s, cx)
    svy = _ty(rky, s, cy)
    r = float(rn[1]) * s
    return '<circle cx="{}" cy="{}" r="{}" stroke="{}" stroke-width="{}" fill="{}"/>'.format(
        _fmt(svx), _fmt(svy), _fmt(r), _COL_BODY, _fmt(_sw(node, s)), _fill(node)
    )


def _prim_pin(node: list, s: float, cx: float, cy: float, rot: float) -> str:
    at = _find(node, "at")
    if not at or len(at) < 4:
        return ""
    px, py = float(at[1]), float(at[2])
    rpx, rpy = _rot(px, py, rot)
    angle = float(at[3]) + rot          # pin angle + symbol rotation
    length = 1.27
    for ch in node:
        if isinstance(ch, list) and ch and ch[0] == "length":
            length = float(ch[1]); break
    ex_k = rpx + math.cos(math.radians(angle)) * length
    ey_k = rpy + math.sin(math.radians(angle)) * length
    svx1, svy1 = _tx(rpx, s, cx), _ty(rpy, s, cy)
    svx2, svy2 = _tx(ex_k, s, cx), _ty(ey_k, s, cy)
    parts = [
        '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" stroke-width="1.5"/>'.format(
            _fmt(svx1), _fmt(svy1), _fmt(svx2), _fmt(svy2), _COL_BODY
        )
    ]

    name = _find(node, "name")
    number = _find(node, "number")
    pin_name = ""
    if name and len(name) > 1:
        pin_name = str(name[1]).strip()
    if number and len(number) > 1 and pin_name == str(number[1]).strip():
        pin_name = ""

    if pin_name:
        dx = svx2 - svx1
        dy = svy2 - svy1
        dist = math.hypot(dx, dy) or 1.0
        ux = dx / dist
        uy = dy / dist
        label_x = svx2 + ux * 6.0
        label_y = svy2 + uy * 6.0
        if abs(ux) >= abs(uy):
            anchor = "start" if ux > 0 else "end"
        else:
            anchor = "middle"
        parts.append(
            '<text x="{}" y="{}" fill="{}" font-size="9.0" font-family="{}" '
            'text-anchor="{}" dominant-baseline="middle">{}</text>'.format(
                _fmt(label_x), _fmt(label_y), _COL_BODY, _FONT, anchor,
                _html_mod.escape(pin_name),
            )
        )

    return "".join(parts)

# ---------------------------------------------------------------------------
# Symbol geometry traversal
# ---------------------------------------------------------------------------

def _graphics(node: list) -> List[list]:
    out: List[list] = []
    for ch in node:
        if not isinstance(ch, list):
            continue
        if ch[0] in ("rectangle", "polyline", "arc", "circle"):
            out.append(ch)
        elif ch[0] == "symbol":
            out.extend(_graphics(ch))
    return out


def _pins(node: list) -> List[list]:
    out: List[list] = []
    for ch in node:
        if not isinstance(ch, list):
            continue
        if ch[0] == "pin":
            out.append(ch)
        elif ch[0] == "symbol":
            out.extend(_pins(ch))
    return out

# ---------------------------------------------------------------------------
# Library cache
# ---------------------------------------------------------------------------

_cache: Dict[str, Any] = {}
# Global index: sym_name_lower -> (lib_name, sym_name)
_sym_index: Dict[str, tuple] = {}
_index_built = False


def _load_lib(lib_name: str) -> Optional[list]:
    if lib_name in _cache:
        return _cache[lib_name]
    path = os.path.join(_LIBS_DIR, lib_name + ".kicad_sym")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    tree, _ = _parse_sexp(_tokenize(text), 0)
    _cache[lib_name] = tree
    return tree


def _build_index() -> None:
    """Build a name→(lib, sym) index over ALL .kicad_sym files (lazy, once)."""
    global _index_built
    if _index_built:
        return
    _index_built = True
    if not os.path.isdir(_LIBS_DIR):
        return
    symbol_re = re.compile(r"\(\s*symbol\s+(?:\"([^\"]+)\"|([^\s()]+))")
    for fname in os.listdir(_LIBS_DIR):
        if not fname.endswith(".kicad_sym"):
            continue
        lib = fname[:-len(".kicad_sym")]
        path = os.path.join(_LIBS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        seen: Set[str] = set()
        for match in symbol_re.finditer(text):
            sname = match.group(1) or match.group(2)
            if not sname or sname in seen:
                continue
            seen.add(sname)
            _sym_index[sname.lower()] = (lib, sname)


def _normalize_symbol_name(sym_name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (sym_name or "").strip().lower())
    return cleaned.strip("_")


def _symbol_tokens(sym_name: str) -> List[str]:
    return [token for token in _normalize_symbol_name(sym_name).split("_") if token]


def _symbol_match_score(query: str, candidate: str) -> Optional[Tuple[int, int, int, int]]:
    if not query or not candidate:
        return None

    if query == candidate:
        return (0, 0, 0, 0)

    q_tokens = _symbol_tokens(query)
    c_tokens = _symbol_tokens(candidate)
    if not q_tokens or not c_tokens:
        return None

    q_compact = re.sub(r"[^a-z0-9]+", "", query.lower())
    c_compact = re.sub(r"[^a-z0-9]+", "", candidate.lower())
    if not q_compact or not c_compact:
        return None

    if q_compact == c_compact:
        return (0, 0, 0, 0)

    if min(len(q_compact), len(c_compact)) >= 3 and (c_compact.startswith(q_compact) or q_compact.startswith(c_compact)):
        return (1, abs(len(c_compact) - len(q_compact)), len(c_tokens), len(q_tokens))

    if min(len(q_compact), len(c_compact)) >= 3 and (q_compact in c_compact or c_compact in q_compact):
        return (2, abs(len(c_compact) - len(q_compact)), len(c_tokens), len(q_tokens))

    shared = [tok for tok in q_tokens if len(tok) >= 3 and tok in c_tokens]
    if shared:
        return (3, -len(shared), abs(len(c_tokens) - len(q_tokens)), abs(len(c_compact) - len(q_compact)))

    return None


def _fuzzy_lookup(sym_name: str) -> Optional[tuple]:
    """
    Find best (lib, sym_name) for sym_name across all KiCad libraries.
    Strategy (in order):
      1. Exact match (case-insensitive)
      2. Prefix / suffix / substring matches
      3. Token overlap on normalized symbol names
    Returns None if nothing found.
    """
    _build_index()
    key = _normalize_symbol_name(sym_name)
    if not key:
        return None

    # 1. Exact, after normalization
    for k, v in _sym_index.items():
        if _normalize_symbol_name(k) == key:
            return v

    candidates: List[Tuple[Tuple[int, int, int, int], str, tuple]] = []
    for k, v in _sym_index.items():
        score = _symbol_match_score(key, k)
        if score is not None:
            candidates.append((score, k, v))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]
    return None


def _sym_node(lib_name: str, sym_name: str) -> Optional[list]:
    alias = _SYMBOL_ALIASES.get((lib_name, sym_name))
    if alias:
        lib_name, sym_name = alias
    # 1. Try specified lib first
    tree = _load_lib(lib_name)
    if tree is not None:
        for ch in tree:
            if isinstance(ch, list) and ch and ch[0] == "symbol" and ch[1] == sym_name:
                return ch
    # 2. Fuzzy / cross-lib lookup
    result = _fuzzy_lookup(sym_name)
    if result:
        found_lib, found_sym = result
        tree2 = _load_lib(found_lib)
        if tree2:
            for ch in tree2:
                if isinstance(ch, list) and ch and ch[0] == "symbol" and ch[1] == found_sym:
                    return ch
    return None


# ---------------------------------------------------------------------------
# Custom symbol cache  (AI-reconstructed symbols stored as JSON)
# ---------------------------------------------------------------------------

def save_custom_symbol(name: str, data: dict) -> None:
    """
    Persist a custom symbol definition to the plugin's cache.
    data expected keys: 'description', 'source_url', 'pins' (dict str→str).
    """
    os.makedirs(_CUSTOM_DIR, exist_ok=True)
    path = os.path.join(_CUSTOM_DIR, name.upper() + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_custom_symbol(name: str) -> Optional[dict]:
    """
    Load a custom symbol definition from the plugin's cache.
    Returns dict with 'pins' key, or None.
    """
    os.makedirs(_CUSTOM_DIR, exist_ok=True)
    path = os.path.join(_CUSTOM_DIR, name.upper() + ".json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# Public: render_symbol_group
# ---------------------------------------------------------------------------

def render_symbol_group(
    lib_name: str,
    sym_name: str,
    cx: float = 0,
    cy: float = 0,
    rotation: float = 0,
    scale: float = 10.0,
) -> str:
    """
    Return an SVG <g> element for the symbol, centred at (cx, cy) in SVG pixels.
    rotation: degrees CCW of the symbol in KiCad space (0 = vertical passive).
    """
    node = _sym_node(lib_name, sym_name)
    if node is None:
        return ""
    parts: List[str] = []
    for prim in _graphics(node):
        tag = prim[0]
        if tag == "rectangle":
            parts.append(_prim_rectangle(prim, scale, cx, cy, rotation))
        elif tag == "polyline":
            parts.append(_prim_polyline(prim, scale, cx, cy, rotation))
        elif tag == "arc":
            parts.append(_prim_arc(prim, scale, cx, cy, rotation))
        elif tag == "circle":
            parts.append(_prim_circle(prim, scale, cx, cy, rotation))
    for pin in _pins(node):
        parts.append(_prim_pin(pin, scale, cx, cy, rotation))
    body = "\n  ".join(p for p in parts if p)
    return "<g>\n  {}\n</g>".format(body)

# ---------------------------------------------------------------------------
# Public: get_pin_positions
# ---------------------------------------------------------------------------

def get_pin_positions(
    lib_name: str,
    sym_name: str,
    cx: float = 0,
    cy: float = 0,
    rotation: float = 0,
    scale: float = 10.0,
) -> Dict[str, Tuple[float, float]]:
    """Return {pin_number: (svg_x, svg_y)} for the connection points of a symbol."""
    node = _sym_node(lib_name, sym_name)
    if node is None:
        return {}
    result: Dict[str, Tuple[float, float]] = {}
    for pin in _pins(node):
        at = _find(pin, "at")
        num = _find(pin, "number")
        if not at or len(at) < 3:
            continue
        pin_num = str(int(float(num[1]))) if num and len(num) > 1 else "?"
        kx, ky = _rot(float(at[1]), float(at[2]), rotation)
        result[pin_num] = (_tx(kx, scale, cx), _ty(ky, scale, cy))
    return result


def get_pin_names(
    lib_name: str,
    sym_name: str,
    rotation: float = 0,
) -> Dict[str, str]:
    """Return {pin_number: pin_name} for a KiCad symbol."""
    node = _sym_node(lib_name, sym_name)
    if node is None:
        return {}
    result: Dict[str, str] = {}
    for pin in _pins(node):
        num = _find(pin, "number")
        name = _find(pin, "name")
        if not num or len(num) < 2:
            continue
        pin_num = str(int(float(num[1]))) if num and len(num) > 1 else "?"
        pin_name = ""
        if name and len(name) > 1:
            pin_name = str(name[1]).strip()
        if pin_name:
            result[pin_num] = pin_name
    return result


def get_pin_directions(
    lib_name: str,
    sym_name: str,
    rotation: float = 0,
) -> Dict[str, Tuple[float, float]]:
    """Return outward SVG unit directions for KiCad symbol pins.

    KiCad stores each pin at its electrical connection point, with the pin
    length extending inward toward the symbol body.  The returned vectors point
    away from the symbol body, which is the direction a schematic wire should
    leave the pin.
    """
    node = _sym_node(lib_name, sym_name)
    if node is None:
        return {}

    result: Dict[str, Tuple[float, float]] = {}
    for pin in _pins(node):
        at = _find(pin, "at")
        num = _find(pin, "number")
        if not at or len(at) < 4:
            continue
        pin_num = str(int(float(num[1]))) if num and len(num) > 1 else "?"
        angle = math.radians(float(at[3]) + rotation)
        ux = -math.cos(angle)
        uy = math.sin(angle)
        if abs(ux) >= abs(uy):
            result[pin_num] = (-1.0 if ux < 0 else 1.0, 0.0)
        else:
            result[pin_num] = (0.0, -1.0 if uy < 0 else 1.0)
    return result

# ---------------------------------------------------------------------------
# Generic IC fallback (used when a symbol is not in KiCad libraries)
# ---------------------------------------------------------------------------

_PIN_SPACING  = 26    # px between adjacent pins on the same side
_STUB_LEN     = 22    # horizontal stub length outside the body
_BODY_MIN_W   = 80    # minimum body width
_PIN_FS       = 9     # font-size for pin names inside box
_CHAR_W       = 5.4   # estimated char width at _PIN_FS (monospace)
_TEXT_PAD     = 5     # padding from box wall to pin-name text


def _ic_layout(
    pin_names: Dict[str, str],
    cx: float,
    cy: float,
) -> Tuple[Dict[str, Tuple[float, float]], float, float, float, float, int]:
    """
    Compute the bounding box and pin connection points for a generic/custom IC.
    First ceil(n/2) pins on the left (top→bottom),
    remaining pins on the right (top→bottom).

    Returns (positions, bx, by, box_w, box_h, left_n).
    Positions are the connection points at the *tip* of each stub.
    """
    n_pins  = max(int(k) for k in pin_names) if pin_names else 2
    left_n  = (n_pins + 1) // 2
    right_n = n_pins - left_n
    n_max   = max(left_n, right_n, 1)
    box_h   = (n_max + 1) * _PIN_SPACING

    # Width: accommodate longest pin name on each side
    left_names  = [pin_names.get(str(i + 1), str(i + 1)) for i in range(left_n)]
    right_names = [pin_names.get(str(left_n + i + 1), str(left_n + i + 1))
                   for i in range(right_n)]
    max_l_ch = max((len(n) for n in left_names),  default=3)
    max_r_ch = max((len(n) for n in right_names), default=3)
    box_w = max(_BODY_MIN_W, (max_l_ch + max_r_ch + 3) * _CHAR_W + 2 * _TEXT_PAD)

    bx = cx - box_w / 2
    by = cy - box_h / 2

    positions: Dict[str, Tuple[float, float]] = {}
    for i in range(left_n):
        y = by + (i + 1) * box_h / (left_n + 1)
        positions[str(i + 1)] = (bx - _STUB_LEN, y)
    for i in range(right_n):
        y = by + (i + 1) * box_h / (right_n + 1)
        positions[str(left_n + i + 1)] = (bx + box_w + _STUB_LEN, y)

    return positions, bx, by, box_w, box_h, left_n


def _draw_ic_body(
    sym_name: str,
    pin_names: Dict[str, str],
    cx: float,
    cy: float,
) -> str:
    """
    Core renderer shared by render_generic_ic and render_custom_ic.
    Pin names are drawn *inside* the body, adjacent to their stub.
    """
    positions, bx, by, box_w, box_h, left_n = _ic_layout(pin_names, cx, cy)
    parts: List[str] = []

    # ── Body rectangle ───────────────────────────────────────────────────
    parts.append(
        '<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" '
        'stroke="{}" stroke-width="1.5" fill="{}"/>'.format(
            bx, by, box_w, box_h, _COL_BODY, _COL_FILL,
        )
    )

    # ── Symbol name at the top centre, inside ────────────────────────────
    parts.append(
        '<text x="{:.1f}" y="{:.1f}" fill="{}" font-size="9" font-weight="bold" '
        'font-family="{}" text-anchor="middle" dominant-baseline="hanging">'
        '{}</text>'.format(
            cx, by + 4, _COL_BODY, _FONT, _html_mod.escape(sym_name)
        )
    )

    # ── Pins ─────────────────────────────────────────────────────────────
    for pin_str, (px, py) in positions.items():
        pnum   = int(pin_str)
        label  = pin_names.get(pin_str, pin_str)
        on_left = pnum <= left_n
        x_wall  = bx if on_left else bx + box_w

        # Stub outside box (horizontal line from box wall to connection point)
        parts.append(
            '<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
            'stroke="{}" stroke-width="1.5"/>'.format(
                x_wall, py, px, py, _COL_BODY,
            )
        )

        # Pin name INSIDE the body, anchored to the wall
        text_x = x_wall + _TEXT_PAD if on_left else x_wall - _TEXT_PAD
        anchor = "start" if on_left else "end"
        parts.append(
            '<text x="{:.1f}" y="{:.1f}" fill="{}" font-size="{}" '
            'font-family="{}" text-anchor="{}" dominant-baseline="middle">'
            '{}</text>'.format(
                text_x, py, _COL_BODY, _PIN_FS, _FONT, anchor,
                _html_mod.escape(label),
            )
        )

    return "<g>{}</g>".format("".join(parts))


def get_generic_pin_positions(
    n_pins: int,
    cx: float,
    cy: float,
) -> Dict[str, Tuple[float, float]]:
    """Pin connection points for a generic IC with n_pins numbered pins."""
    names = {str(i + 1): str(i + 1) for i in range(n_pins)}
    positions, _, _, _, _, _ = _ic_layout(names, cx, cy)
    return positions


def get_custom_pin_positions(
    pin_names: Dict[str, str],
    cx: float,
    cy: float,
) -> Dict[str, Tuple[float, float]]:
    """Pin connection points for a custom symbol loaded from the AI cache."""
    positions, _, _, _, _, _ = _ic_layout(pin_names, cx, cy)
    return positions


def render_generic_ic(
    sym_name: str,
    n_pins: int,
    cx: float,
    cy: float,
) -> str:
    """SVG <g> for an unknown IC (numbered pins inside body)."""
    names = {str(i + 1): str(i + 1) for i in range(n_pins)}
    return _draw_ic_body(sym_name, names, cx, cy)


def render_custom_ic(
    sym_name: str,
    pin_names: Dict[str, str],
    cx: float,
    cy: float,
) -> str:
    """SVG <g> for a custom AI-cached symbol (named pins inside body)."""
    return _draw_ic_body(sym_name, pin_names, cx, cy)



# ---------------------------------------------------------------------------
# Circuit DSL  →  full standalone SVG
# ---------------------------------------------------------------------------
# Grammar (one instruction per line, # comments allowed):
#
#   canvas <w> <h>               optional, default 400×300
#   bg <hex_color>               optional, default #1a1d22
#   sym <ref> <Lib:Name> <cx> <cy> [rotation=0] ["ref_label"] ["val_label"]
#   wire <x1> <y1> <x2> <y2>
#   vcc <x> <y> [label]
#   gnd <x> <y>
#   dot <x> <y>                  junction dot
#   label <x> <y> <text>
#
# Coordinates are in SVG pixels.  scale is always 10 px/mm.
# ---------------------------------------------------------------------------

_SCALE = 10.0
_PIN_DIST = 38.1   # px: distance from center to pin connection for R/C/L (3.81mm × 10)


def _parse_quoted_or_token(tokens: List[str], idx: int) -> Tuple[str, int]:
    """Return (value, next_idx). Handles quoted tokens assembled from the raw line."""
    if idx >= len(tokens):
        return "", idx
    return tokens[idx], idx + 1


def _vcc_svg(x: float, y: float, label: str = "VCC") -> str:
    """VCC power symbol — connection point at (x, y)."""
    tip_y = y - 22
    return (
        '<line x1="{x}" y1="{y}" x2="{x}" y2="{ty}" stroke="{c}" stroke-width="1.5"/>'
        '<polygon points="{x1},{ty} {x2},{ty} {x},{pty}" fill="{c}"/>'
        '<text x="{tx}" y="{tty}" fill="{ct}" font-size="{fs}" font-family="{ff}">{lbl}</text>'
    ).format(
        x=_fmt(x), y=_fmt(y), ty=_fmt(tip_y),
        x1=_fmt(x - 8), x2=_fmt(x + 8), pty=_fmt(tip_y - 12),
        tx=_fmt(x + 12), tty=_fmt(tip_y - 2),
        c=_COL_BODY, ct=_COL_TEXT_REF,
        fs=_FONT_SIZE, ff=_FONT, lbl=label,
    )


def _gnd_svg(x: float, y: float) -> str:
    """GND power symbol — connection point at (x, y)."""
    return (
        '<line x1="{x}" y1="{y}" x2="{x}" y2="{y2}" stroke="{c}" stroke-width="1.5"/>'
        '<line x1="{a1}" y1="{y2}" x2="{a2}" y2="{y2}" stroke="{c}" stroke-width="2.0"/>'
        '<line x1="{b1}" y1="{y3}" x2="{b2}" y2="{y3}" stroke="{c}" stroke-width="1.5"/>'
        '<line x1="{d1}" y1="{y4}" x2="{d2}" y2="{y4}" stroke="{c}" stroke-width="1.0"/>'
    ).format(
        x=_fmt(x), y=_fmt(y), y2=_fmt(y + 12),
        y3=_fmt(y + 18), y4=_fmt(y + 24),
        a1=_fmt(x - 12), a2=_fmt(x + 12),
        b1=_fmt(x - 8), b2=_fmt(x + 8),
        d1=_fmt(x - 4), d2=_fmt(x + 4),
        c=_COL_BODY,
    )


def _wire_svg(x1: float, y1: float, x2: float, y2: float) -> str:
    return '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" stroke-width="1.5"/>'.format(
        _fmt(x1), _fmt(y1), _fmt(x2), _fmt(y2), _COL_BODY
    )


def _dot_svg(x: float, y: float) -> str:
    return '<circle cx="{}" cy="{}" r="3" fill="{}"/>'.format(_fmt(x), _fmt(y), _COL_BODY)


def _label_svg(x: float, y: float, text: str) -> str:
    import html as _html
    return '<text x="{}" y="{}" fill="{}" font-size="{}" font-family="{}">{}</text>'.format(
        _fmt(x), _fmt(y), _COL_BODY, _FONT_SIZE, _FONT, _html.escape(text)
    )


def _sym_label_svg(cx: float, cy: float, ref: str, val: str) -> str:
    import html as _html
    return (
        '<text x="{rx}" y="{ry}" fill="{cr}" font-size="{fs}" font-family="{ff}">{ref}</text>'
        '<text x="{vx}" y="{vy}" fill="{cv}" font-size="{fs}" font-family="{ff}">{val}</text>'
    ).format(
        rx=_fmt(cx + 14), ry=_fmt(cy - 6),
        vx=_fmt(cx + 14), vy=_fmt(cy + 10),
        cr=_COL_TEXT_REF, cv=_COL_TEXT_VAL,
        fs=_FONT_SIZE - 1, ff=_FONT,
        ref=_html.escape(ref), val=_html.escape(val),
    )


def render_circuit_dsl(dsl_text: str) -> str:
    """
    Parse a circuit DSL block and return a complete standalone SVG string.

    Supported primitives
    --------------------
    canvas  <w> <h>
    bg      <hex>
    sym     <Ref> <Lib:Name> <cx> <cy>  [rotation=0]  [ref_label]  [val_label]
    wire    <x1> <y1> <x2> <y2>
    vcc     <x> <y>  [label=VCC]
    gnd     <x> <y>
    dot     <x> <y>
    label   <x> <y>  <text>
    """
    width, height = 400.0, 300.0
    bg = _COL_BG
    parts: List[str] = []

    for raw_line in dsl_text.split("\n"):
        line = raw_line.split("#")[0].strip()
        if not line:
            continue
        tokens = line.split()
        cmd = tokens[0].lower()

        try:
            if cmd == "canvas" and len(tokens) >= 3:
                width, height = float(tokens[1]), float(tokens[2])

            elif cmd == "bg" and len(tokens) >= 2:
                bg = tokens[1]

            elif cmd == "sym" and len(tokens) >= 5:
                ref = tokens[1]
                lib_sym = tokens[2]
                cx, cy = float(tokens[3]), float(tokens[4])
                rotation = float(tokens[5]) if len(tokens) > 5 and tokens[5].lstrip("-").replace(".", "").isdigit() else 0.0
                # parse optional labels (may have quotes stripped by split)
                label_start = 6 if len(tokens) > 5 and tokens[5].lstrip("-").replace(".", "").isdigit() else 5
                ref_label = tokens[label_start] if len(tokens) > label_start else ref
                val_label = tokens[label_start + 1] if len(tokens) > label_start + 1 else ""
                ref_label = ref_label.strip('"')
                val_label = val_label.strip('"')

                if ":" in lib_sym:
                    lib_name, sym_name = lib_sym.split(":", 1)
                else:
                    lib_name = sym_name = lib_sym

                g = render_symbol_group(lib_name, sym_name, cx, cy, rotation, _SCALE)
                if g:
                    parts.append(g)
                    parts.append(_sym_label_svg(cx, cy, ref_label, val_label))
                else:
                    # fallback: simple rectangle placeholder
                    parts.append('<rect x="{}" y="{}" width="20" height="50" stroke="{}" stroke-width="1.5" fill="none"/>'.format(
                        _fmt(cx - 10), _fmt(cy - 25), _COL_BODY))
                    parts.append(_sym_label_svg(cx, cy, ref_label, val_label))

            elif cmd == "wire" and len(tokens) >= 5:
                parts.append(_wire_svg(float(tokens[1]), float(tokens[2]),
                                       float(tokens[3]), float(tokens[4])))

            elif cmd == "vcc" and len(tokens) >= 3:
                lbl = tokens[3].strip('"') if len(tokens) > 3 else "VCC"
                parts.append(_vcc_svg(float(tokens[1]), float(tokens[2]), lbl))

            elif cmd == "gnd" and len(tokens) >= 3:
                parts.append(_gnd_svg(float(tokens[1]), float(tokens[2])))

            elif cmd == "dot" and len(tokens) >= 3:
                parts.append(_dot_svg(float(tokens[1]), float(tokens[2])))

            elif cmd == "label" and len(tokens) >= 4:
                text = " ".join(tokens[3:]).strip('"')
                parts.append(_label_svg(float(tokens[1]), float(tokens[2]), text))

        except (ValueError, IndexError):
            continue  # skip malformed lines

    body = "\n  ".join(parts)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="{w}" height="{h}" viewBox="0 0 {w} {h}">\n'
        '  <rect width="100%" height="100%" fill="{bg}"/>\n'
        '  {body}\n'
        '</svg>'
    ).format(w=_fmt(width), h=_fmt(height), bg=bg, body=body)
