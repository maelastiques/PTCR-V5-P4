"""
Semantic KiCad-style schematic SVG renderer.

Input syntax:

    Ref  Lib:Symbol  Value  pin1_net  pin2_net  [pin3_net ...]  [key=value ...]
    label  net_name  display_text
    net    net_name  role=bus|signal|power  label=Text

Component metadata understood by the layout engine:

    role=main|connector|input|output|source|load
    role=bypass|decouple|bulk|pullup|pulldown|series|filter|termination
    role=clock|crystal|bus|none
    near=U1.8:100,U2.4:70
    side=left|right|top|bottom

The renderer intentionally prefers schematic readability over physical
topology. Power nets are shown with local power symbols, bus-like signals are
usually shown with local labels, and parts with roles/near hints are placed
as satellites around the pin they serve.
"""

from __future__ import annotations

import html as _html
import math
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .sym_renderer import (
    get_custom_pin_positions,
    get_generic_pin_positions,
    get_pin_directions,
    get_pin_names,
    get_pin_positions,
    load_custom_symbol,
    render_custom_ic,
    render_generic_ic,
    render_symbol_group,
    save_custom_symbol,
)


# ---------------------------------------------------------------------------
# KiCad-ish light theme and geometry constants
# ---------------------------------------------------------------------------

SYM_SCALE = 10.0

BG = "#f5f4ef"
GRID = "#cbc9c2"
WIRE = "#009600"
BODY = "#840000"
POWER = BODY
REF = "#006464"
VAL = "#006464"
LABEL = "#0f0f0f"
FONT = "SF Mono, ui-monospace, Courier New, monospace"

MARGIN_X = 110.0
MARGIN_Y = 105.0
CANVAS_PAD_X = 90.0
CANVAS_PAD_Y = 60.0
ANCHOR_X_GAP = 330.0
ANCHOR_Y_GAP = 210.0
SIDE_STACK_STEP = 165.0
FREE_GRID_X = 190.0
FREE_GRID_Y = 135.0
PIN_ESCAPE = 16.0
WIRE_LANE = 26.0
LOCAL_WIRE_GAP = 10.0
POWER_SYMBOL_LEAD = 26.0


_VCC_RE = re.compile(
    r"^(VCC|VDD|\+V|\+[0-9]|PWR|AVCC|DVCC|IOVDD|VBAT|VSUP|VBUS|V_[0-9]|"
    r"\+3V|\+5V|\+12V|3V3|5V|12V|VMAIN|VIN|VOUT_PWR)",
    re.I,
)
_GND_RE = re.compile(r"^(GND|VSS|AGND|DGND|PGND|EARTH|0V|GND_|SGND|-V|-[0-9])", re.I)
_BUS_RE = re.compile(
    r"(SPI|I2C|I2S|UART|USB|CAN|SDIO|ETH|MIPI|LVDS|HDMI|SDA|SCL|SCK|MOSI|MISO|RXD?|TXD?|D\+|D-|CLK)",
    re.I,
)


def _is_vcc(name: str) -> bool:
    return bool(_VCC_RE.match(name or ""))


def _is_gnd(name: str) -> bool:
    return bool(_GND_RE.match(name or ""))


def _is_power(name: str) -> bool:
    return _is_vcc(name) or _is_gnd(name)


def _is_nc_net(name: str) -> bool:
    cleaned = re.sub(r"[^A-Z0-9]", "", (name or "").upper())
    return cleaned in {"NC", "NOCONNECT", "UNCONNECTED", "DNC", "DO NOT CONNECT".replace(" ", "")}


def _clean_net_name(name: str) -> str:
    return name.strip().strip('"')


def _norm_net_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def _is_power_pin_name(name: str) -> bool:
    return _norm_net_key(name) in {"GND", "VSS", "VDD", "VCC", "3V3", "3V0", "5V", "1V8", "0V", "AGND", "DGND", "PGND", "VIN"}


def _remap_component_nets_to_symbol_pins(comp: "Comp") -> None:
    pin_names = get_pin_names(comp.lib, comp.sym, comp.rotation)
    if not pin_names or not comp.nets:
        return

    ordered_pins = sorted(comp.nets)
    ordered_nets = [comp.nets[pin] for pin in ordered_pins]
    remapped: Dict[int, str] = {}
    matched_keys: Set[str] = set()

    for pin in ordered_pins:
        pname = pin_names.get(str(pin), "")
        if not pname:
            continue
        target_key = _norm_net_key(pname)
        if not target_key:
            continue
        for net_name in ordered_nets:
            if _norm_net_key(net_name) == target_key:
                remapped[pin] = net_name
                matched_keys.add(target_key)
                break

    leftovers = [net_name for net_name in ordered_nets if _norm_net_key(net_name) not in matched_keys]
    for pin in ordered_pins:
        if pin not in remapped:
            remapped[pin] = leftovers.pop(0) if leftovers else comp.nets[pin]

    comp.nets = remapped


def _norm_role(role: str) -> str:
    role = (role or "none").strip().lower().replace("-", "_")
    aliases = {
        "decouple": "bypass",
        "decoupling": "bypass",
        "decoupler": "bypass",
        "bypass_cap": "bypass",
        "bulk_cap": "bulk",
        "pull": "pullup",
        "pull_up": "pullup",
        "pull_down": "pulldown",
        "input_connector": "input",
        "output_connector": "output",
        "conn": "connector",
        "io": "connector",
        "ic": "main",
        "mcu": "main",
        "processor": "main",
        "driver": "main",
        "terminator": "termination",
        "term": "termination",
        "xtal": "crystal",
    }
    return aliases.get(role, role or "none")


def _is_capacitor(comp: "Comp") -> bool:
    return comp.ref[:1].upper() == "C" or comp.sym.upper().startswith("C")


def _is_resistor(comp: "Comp") -> bool:
    return comp.ref[:1].upper() == "R" or comp.sym.upper().startswith("R")


def _is_inductor(comp: "Comp") -> bool:
    return comp.ref[:1].upper() == "L" or comp.sym.upper().startswith("L")


def _is_diode(comp: "Comp") -> bool:
    return comp.ref[:1].upper() == "D" or comp.sym.upper().startswith(("D", "LED", "ZENER"))


def _is_passive(comp: "Comp") -> bool:
    return _is_capacitor(comp) or _is_resistor(comp) or _is_inductor(comp) or _is_diode(comp)


def _is_two_pin(comp: "Comp") -> bool:
    return len(comp.nets) == 2


def _is_connector_like(comp: "Comp") -> bool:
    prefix = comp.ref[:1].upper()
    return comp.role in {"connector", "input", "output", "source", "load"} or prefix in {"J", "P", "K", "X"}


def _is_bypass(comp: "Comp") -> bool:
    if comp.role in {"bypass", "bulk"}:
        return True
    if not _is_capacitor(comp) or not _is_two_pin(comp):
        return False
    nets = list(comp.nets.values())
    return any(_is_vcc(n) for n in nets) and any(_is_gnd(n) for n in nets)


def _grounded_cap_pins(comp: "Comp") -> Optional[Tuple[int, int]]:
    if not _is_capacitor(comp) or not _is_two_pin(comp):
        return None
    gnd_pin = next((pin for pin, net_name in sorted(comp.nets.items()) if _is_gnd(net_name)), 0)
    if not gnd_pin:
        return None
    other_pin = next((pin for pin in sorted(comp.nets) if pin != gnd_pin), 0)
    if not other_pin:
        return None
    return gnd_pin, other_pin


def _is_ground_shunt_cap(comp: "Comp") -> bool:
    pins = _grounded_cap_pins(comp)
    if pins is None:
        return False
    _gnd_pin, other_pin = pins
    return not _is_power(comp.nets.get(other_pin, ""))


def _is_pull(comp: "Comp") -> bool:
    if comp.role in {"pullup", "pulldown"}:
        return True
    if not _is_resistor(comp) or not _is_two_pin(comp):
        return False
    nets = list(comp.nets.values())
    return any(_is_power(n) for n in nets) and any(not _is_power(n) for n in nets)


def _is_local_role(comp: "Comp") -> bool:
    return comp.role in {
        "bypass",
        "bulk",
        "pullup",
        "pulldown",
        "series",
        "filter",
        "termination",
        "feedback",
        "sense",
        "clock",
        "crystal",
    } or _is_bypass(comp) or _is_pull(comp) or _is_ground_shunt_cap(comp)


def _detect_bus_like(net: "Net") -> bool:
    if net.role == "bus":
        return True
    if _BUS_RE.search(net.name or ""):
        return True
    return False


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Net:
    name: str
    display: str = ""
    role: str = "signal"
    has_label: bool = False


@dataclass
class NearHint:
    ref: str
    pin: Optional[int] = None
    strength: int = 100


@dataclass
class Comp:
    ref: str
    lib: str
    sym: str
    value: str
    nets: Dict[int, str] = field(default_factory=dict)
    attrs: Dict[str, str] = field(default_factory=dict)
    role: str = "none"
    near: List[NearHint] = field(default_factory=list)
    bend: str = "allow"
    side_hint: str = ""

    cx: float = 0.0
    cy: float = 0.0
    rotation: float = 0.0
    placement: str = "free"
    anchor_ref: str = ""
    anchor_pin: int = 0
    placed: bool = False


@dataclass
class Endpoint:
    comp: Comp
    pin: int
    x: float
    y: float
    side: str
    ex: float
    ey: float


@dataclass
class SvgState:
    parts: List[str] = field(default_factory=list)
    bounds: List[Tuple[float, float, float, float]] = field(default_factory=list)
    segments: List[Tuple[str, float, float, float, float]] = field(default_factory=list)

    def add(self, svg: str, bounds: Optional[Tuple[float, float, float, float]] = None) -> None:
        if svg:
            self.parts.append(svg)
        if bounds is not None:
            self.bounds.append(bounds)

    def add_segment(self, net_name: str, x1: float, y1: float, x2: float, y2: float) -> None:
        if net_name:
            self.segments.append((net_name, x1, y1, x2, y2))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _tokenize(line: str) -> List[str]:
    try:
        return shlex.split(line, comments=False, posix=True)
    except Exception:
        return line.split()


def _parse_near(value: str) -> List[NearHint]:
    hints: List[NearHint] = []
    for raw in re.split(r"[;,]", value or ""):
        item = raw.strip()
        if not item:
            continue

        strength = 100
        if ":" in item:
            item, strength_text = item.rsplit(":", 1)
            try:
                strength = int(float(strength_text.strip()))
            except Exception:
                strength = 100
        elif "@" in item:
            item, strength_text = item.rsplit("@", 1)
            try:
                strength = int(float(strength_text.strip()))
            except Exception:
                strength = 100

        ref = item.strip()
        pin: Optional[int] = None
        if "." in ref:
            ref, pin_text = ref.split(".", 1)
            try:
                pin = int(pin_text.strip())
            except Exception:
                pin = None

        if ref:
            hints.append(NearHint(ref=ref.strip(), pin=pin, strength=max(1, min(100, strength))))
    return hints


def _parse_attrs(tokens: Sequence[str]) -> Tuple[List[str], Dict[str, str]]:
    nets: List[str] = []
    attrs: Dict[str, str] = {}
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            attrs[key.strip().lower()] = value.strip()
        else:
            nets.append(_clean_net_name(token))
    return nets, attrs


def _parse_component(parts: Sequence[str]) -> Optional[Comp]:
    if len(parts) < 5:
        return None

    ref = parts[0].strip()
    lib_sym = parts[1].strip()
    value = parts[2].strip()
    pin_nets, attrs = _parse_attrs(parts[3:])
    if len(pin_nets) < 1:
        return None

    if ":" not in lib_sym:
        lib_sym = f"Device:{lib_sym}"
    lib, sym = lib_sym.split(":", 1)

    comp = Comp(ref=ref, lib=lib, sym=sym, value=value, attrs=attrs)
    comp.role = _norm_role(attrs.get("role", "none"))
    comp.bend = attrs.get("bend", attrs.get("route", "allow")).strip().lower()
    comp.side_hint = attrs.get("side", attrs.get("placement", "")).strip().lower()
    if "near" in attrs:
        comp.near = _parse_near(attrs["near"])
    if "target" in attrs:
        comp.near.extend(_parse_near(attrs["target"]))
    for idx, net_name in enumerate(pin_nets, 1):
        if _is_nc_net(net_name):
            continue
        comp.nets[idx] = net_name
    _remap_component_nets_to_symbol_pins(comp)
    return comp


def _parse_net_attrs(net: Net, tokens: Sequence[str]) -> None:
    attrs: Dict[str, str] = {}
    free: List[str] = []
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            attrs[key.strip().lower()] = value.strip()
        else:
            free.append(token)
    if "role" in attrs:
        net.role = _norm_role(attrs["role"])
    if "label" in attrs:
        net.display = attrs["label"].strip('"')
        net.has_label = True
    elif free:
        net.display = " ".join(free).strip('"')
        net.has_label = True


def _parse(text: str) -> Tuple[Dict[str, Comp], Dict[str, Net]]:
    comps: Dict[str, Comp] = {}
    nets: Dict[str, Net] = {}

    def ensure_net(name: str) -> Net:
        name = _clean_net_name(name)
        if name not in nets:
            role = "power" if _is_power(name) else "signal"
            nets[name] = Net(name=name, display=name, role=role)
        return nets[name]

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = _tokenize(line)
        if not parts:
            continue

        head = parts[0].lower()
        if head == "label" and len(parts) >= 3:
            if _is_nc_net(parts[1]):
                continue
            net = ensure_net(parts[1])
            net.display = " ".join(parts[2:]).strip('"')
            net.has_label = True
            continue

        if head == "net" and len(parts) >= 2:
            if _is_nc_net(parts[1]):
                continue
            net = ensure_net(parts[1])
            _parse_net_attrs(net, parts[2:])
            continue

        comp = _parse_component(parts)
        if comp is None:
            continue
        for net_name in comp.nets.values():
            ensure_net(net_name)
        comps[comp.ref] = comp

    _infer_roles(comps, nets)
    return comps, nets


def _infer_roles(comps: Dict[str, Comp], nets: Dict[str, Net]) -> None:
    for comp in comps.values():
        if comp.role in {"none", ""}:
            if _is_bypass(comp):
                comp.role = "bypass"
            elif _is_pull(comp):
                power = next((n for n in comp.nets.values() if _is_power(n)), "")
                comp.role = "pulldown" if _is_gnd(power) else "pullup"
            elif _is_connector_like(comp):
                comp.role = "connector"
            elif comp.ref[:1].upper() in {"U", "A", "M"} or len(comp.nets) >= 5:
                comp.role = "main"
            else:
                comp.role = "none"

        if comp.role == "bus":
            for net_name in comp.nets.values():
                if net_name in nets and not _is_power(net_name):
                    nets[net_name].role = "bus"


# ---------------------------------------------------------------------------
# Symbol geometry helpers
# ---------------------------------------------------------------------------


def _pin_map(comp: Comp, cx: Optional[float] = None, cy: Optional[float] = None, rotation: Optional[float] = None) -> Dict[str, Tuple[float, float]]:
    px = comp.cx if cx is None else cx
    py = comp.cy if cy is None else cy
    rot = comp.rotation if rotation is None else rotation

    pins = get_pin_positions(comp.lib, comp.sym, px, py, rot, SYM_SCALE)
    if pins:
        return pins

    custom = load_custom_symbol(comp.sym)
    if custom and custom.get("pins"):
        return get_custom_pin_positions(custom["pins"], px, py)

    n_pins = max(comp.nets.keys()) if comp.nets else 2
    return get_generic_pin_positions(n_pins, px, py)


def _pin_pos(comp: Comp, pin: int) -> Optional[Tuple[float, float]]:
    return _pin_map(comp).get(str(pin))


def _pin_side_from_point(comp: Comp, x: float, y: float) -> str:
    dx = x - comp.cx
    dy = y - comp.cy
    if abs(dx) >= abs(dy):
        return "left" if dx < 0 else "right"
    return "top" if dy < 0 else "bottom"


def _side_from_unit(ux: float, uy: float) -> str:
    if abs(ux) >= abs(uy):
        return "left" if ux < 0 else "right"
    return "top" if uy < 0 else "bottom"


def _pin_outward_side(comp: Comp, pin: int) -> Optional[str]:
    directions = get_pin_directions(comp.lib, comp.sym, comp.rotation)
    direction = directions.get(str(pin))
    if direction is not None:
        return _side_from_unit(direction[0], direction[1])

    custom = load_custom_symbol(comp.sym)
    if custom and custom.get("pins"):
        pos = _pin_pos(comp, pin)
        return _pin_side_from_point(comp, pos[0], pos[1]) if pos is not None else None

    pos = _pin_pos(comp, pin)
    return _pin_side_from_point(comp, pos[0], pos[1]) if pos is not None else None


def _pin_side(comp: Comp, pin: int) -> str:
    side = _pin_outward_side(comp, pin)
    if side is not None:
        return side
    net_name = comp.nets.get(pin, "")
    if _is_vcc(net_name):
        return "top"
    if _is_gnd(net_name):
        return "bottom"
    return "left" if pin <= max(1, len(comp.nets) // 2) else "right"


def _opposite(side: str) -> str:
    return {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}.get(side, "right")


def _unit_for_side(side: str) -> Tuple[float, float]:
    return {
        "left": (-1.0, 0.0),
        "right": (1.0, 0.0),
        "top": (0.0, -1.0),
        "bottom": (0.0, 1.0),
    }.get(side, (1.0, 0.0))


def _tangent_for_side(side: str) -> Tuple[float, float]:
    return {
        "left": (0.0, 1.0),
        "right": (0.0, 1.0),
        "top": (1.0, 0.0),
        "bottom": (1.0, 0.0),
    }.get(side, (0.0, 1.0))


def _rotation_for_pin_side(comp: Comp, pin: int, desired_side: str) -> float:
    if not _is_two_pin(comp) and not _is_passive(comp):
        return comp.rotation

    for rot in (0.0, 90.0, 180.0, 270.0):
        test = Comp(ref=comp.ref, lib=comp.lib, sym=comp.sym, value=comp.value, nets=comp.nets, role=comp.role)
        test.cx = 0.0
        test.cy = 0.0
        test.rotation = rot
        if _pin_outward_side(test, pin) == desired_side:
            return rot
    return comp.rotation


def _default_rotation(comp: Comp) -> float:
    custom = load_custom_symbol(comp.sym)
    if custom and custom.get("pins") and not get_pin_positions(comp.lib, comp.sym, 0.0, 0.0, 0.0, SYM_SCALE):
        return 0.0
    grounded = _grounded_cap_pins(comp)
    if grounded is not None:
        gnd_pin, _other_pin = grounded
        return _rotation_for_pin_side(comp, gnd_pin, "bottom")
    if _is_two_pin(comp) and _is_passive(comp):
        if _is_diode(comp):
            return 0.0
        return 90.0
    return 0.0


def _component_bounds(comp: Comp) -> Tuple[float, float, float, float]:
    pins = list(_pin_map(comp).values())
    if pins:
        left = min(x for x, _ in pins) - 26.0
        right = max(x for x, _ in pins) + 54.0
        top = min(y for _, y in pins) - 28.0
        bottom = max(y for _, y in pins) + 46.0
    else:
        left = comp.cx - 58.0
        right = comp.cx + 58.0
        top = comp.cy - 44.0
        bottom = comp.cy + 58.0

    n_pins = max(comp.nets.keys()) if comp.nets else 2
    has_real_symbol = bool(get_pin_positions(comp.lib, comp.sym, 0.0, 0.0, comp.rotation, SYM_SCALE))
    if not has_real_symbol and load_custom_symbol(comp.sym) and n_pins >= 3:
        body_h = (math.ceil(n_pins / 2) + 1) * 26.0
        top = min(top, comp.cy - body_h / 2 - 20.0)
        bottom = max(bottom, comp.cy + body_h / 2 + 38.0)
    if n_pins >= 6:
        body_h = (math.ceil(n_pins / 2) + 1) * 26.0
        top = min(top, comp.cy - body_h / 2 - 20.0)
        bottom = max(bottom, comp.cy + body_h / 2 + 42.0)
    return left, top, right, bottom


def _scene_bounds(comps: Iterable[Comp]) -> Tuple[float, float, float, float]:
    boxes = [_component_bounds(c) for c in comps]
    if not boxes:
        return 0.0, 0.0, 0.0, 0.0
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _translate(comps: Dict[str, Comp], dx: float, dy: float) -> None:
    if abs(dx) < 0.1 and abs(dy) < 0.1:
        return
    for comp in comps.values():
        comp.cx += dx
        comp.cy += dy


# ---------------------------------------------------------------------------
# Semantic analysis and placement
# ---------------------------------------------------------------------------


def _anchor_score(comp: Comp) -> Tuple[int, int, int, int, str]:
    pin_count = len(comp.nets)
    non_power = sum(1 for n in comp.nets.values() if not _is_power(n))
    active_bonus = 4 if comp.role == "main" else 0
    active_bonus += 2 if comp.ref[:1].upper() in {"U", "A", "M"} else 0
    active_bonus += 1 if pin_count >= 5 else 0
    connector_penalty = 2 if _is_connector_like(comp) else 0
    passive_penalty = 2 if _is_passive(comp) else 0
    return active_bonus, pin_count, non_power, -(connector_penalty + passive_penalty), comp.ref


def _anchor_candidates(comps: Dict[str, Comp]) -> List[Comp]:
    candidates: List[Comp] = []
    for comp in comps.values():
        if _is_local_role(comp):
            continue
        if comp.role in {"main", "connector", "input", "output", "source", "load"}:
            candidates.append(comp)
        elif len(comp.nets) >= 4 or comp.ref[:1].upper() in {"U", "A", "M", "J", "P", "X"}:
            candidates.append(comp)
    return candidates


def _shared_nets(a: Comp, b: Comp, include_power: bool = False) -> List[str]:
    a_nets = set(a.nets.values())
    out = sorted(n for n in b.nets.values() if n in a_nets and (include_power or not _is_power(n)))
    return out


def _best_anchor_for_net(net_name: str, comps: Dict[str, Comp], exclude: str = "") -> Optional[Comp]:
    options: List[Comp] = []
    for comp in comps.values():
        if comp.ref == exclude:
            continue
        if net_name not in comp.nets.values():
            continue
        if _is_bypass(comp) or _is_pull(comp):
            continue
        options.append(comp)
    if not options:
        return None
    options.sort(key=_anchor_score, reverse=True)
    return options[0]


def _first_pin_on_net(comp: Comp, net_name: str) -> int:
    for pin, nn in sorted(comp.nets.items()):
        if nn == net_name:
            return pin
    return 0


def _choose_anchor_pin(comp: Comp, anchor: Comp) -> int:
    if comp.anchor_pin:
        return comp.anchor_pin

    if _is_bypass(comp):
        for net_name in comp.nets.values():
            if _is_vcc(net_name):
                pin = _first_pin_on_net(anchor, net_name)
                if pin:
                    return pin

    if _is_pull(comp):
        for net_name in comp.nets.values():
            if not _is_power(net_name):
                pin = _first_pin_on_net(anchor, net_name)
                if pin:
                    return pin

    for net_name in comp.nets.values():
        if _is_power(net_name):
            continue
        pin = _first_pin_on_net(anchor, net_name)
        if pin:
            return pin

    for net_name in comp.nets.values():
        pin = _first_pin_on_net(anchor, net_name)
        if pin:
            return pin
    return next(iter(anchor.nets.keys()), 1) if anchor.nets else 1


def _target_pin_for_anchor(comp: Comp, anchor: Comp, anchor_pin: int) -> int:
    anchor_net = anchor.nets.get(anchor_pin, "")
    if anchor_net:
        for pin, net_name in sorted(comp.nets.items()):
            if net_name == anchor_net:
                return pin

    if _is_bypass(comp):
        for pin, net_name in sorted(comp.nets.items()):
            if _is_vcc(net_name):
                return pin

    if _is_pull(comp):
        for pin, net_name in sorted(comp.nets.items()):
            if not _is_power(net_name):
                return pin

    return next(iter(comp.nets.keys()), 1) if comp.nets else 1


def _resolve_semantic_anchors(comps: Dict[str, Comp], nets: Dict[str, Net]) -> None:
    for comp in comps.values():
        comp.rotation = _default_rotation(comp)
        comp.anchor_ref = ""
        comp.anchor_pin = 0
        comp.placement = "free"

    for comp in comps.values():
        for hint in comp.near:
            if hint.ref in comps:
                comp.anchor_ref = hint.ref
                comp.anchor_pin = hint.pin or 0
                break

        if _is_bypass(comp):
            comp.placement = "local_power"
            if not comp.anchor_ref:
                power_net = next((n for n in comp.nets.values() if _is_vcc(n)), "")
                anchor = _best_anchor_for_net(power_net, comps, exclude=comp.ref) if power_net else None
                if anchor:
                    comp.anchor_ref = anchor.ref
            continue

        if _is_ground_shunt_cap(comp):
            comp.placement = "local_signal"
            if not comp.anchor_ref:
                signal_net = next((n for n in comp.nets.values() if not _is_power(n)), "")
                anchor = _best_anchor_for_net(signal_net, comps, exclude=comp.ref) if signal_net else None
                if anchor:
                    comp.anchor_ref = anchor.ref
            continue

        if _is_pull(comp):
            comp.placement = "local_signal"
            if not comp.anchor_ref:
                signal_net = next((n for n in comp.nets.values() if not _is_power(n)), "")
                anchor = _best_anchor_for_net(signal_net, comps, exclude=comp.ref) if signal_net else None
                if anchor:
                    comp.anchor_ref = anchor.ref
            continue

        if comp.role in {"series", "filter", "termination", "feedback", "sense", "clock", "crystal", "bulk"}:
            comp.placement = "local_signal"
            if not comp.anchor_ref:
                signal_net = next((n for n in comp.nets.values() if not _is_power(n)), "")
                anchor = _best_anchor_for_net(signal_net, comps, exclude=comp.ref) if signal_net else None
                if anchor:
                    comp.anchor_ref = anchor.ref

    for comp in comps.values():
        if comp.anchor_ref and comp.anchor_ref in comps:
            anchor = comps[comp.anchor_ref]
            comp.anchor_pin = _choose_anchor_pin(comp, anchor)


def _spread_offsets(count: int, step: float) -> List[float]:
    if count <= 0:
        return []
    offsets = [0.0]
    idx = 1
    while len(offsets) < count:
        offsets.append(idx * step)
        if len(offsets) < count:
            offsets.append(-idx * step)
        idx += 1
    return offsets


def _preferred_side_for_anchor(comp: Comp, primary: Comp) -> str:
    if comp.side_hint in {"left", "right", "top", "bottom"}:
        return comp.side_hint
    if comp.role in {"input", "source"}:
        return "left"
    if comp.role in {"output", "load"}:
        return "right"
    if comp.role == "connector":
        ref_value = f"{comp.ref} {comp.value}".upper()
        if any(token in ref_value for token in ("IN", "USB", "PWR", "BAT")):
            return "left"
        if any(token in ref_value for token in ("OUT", "LOAD", "MOTOR", "LED")):
            return "right"

    side_votes: DefaultDict[str, int] = defaultdict(int)
    for net_name in _shared_nets(comp, primary):
        for pin, nn in primary.nets.items():
            if nn == net_name:
                side_votes[_pin_side(primary, pin)] += 1
    if side_votes:
        return max(side_votes.items(), key=lambda item: (item[1], item[0]))[0]

    return "right" if _is_connector_like(comp) else "bottom"


def _place_anchor_shell(anchors: List[Comp]) -> None:
    if not anchors:
        return

    anchors.sort(key=_anchor_score, reverse=True)
    primary = anchors[0]
    primary.cx = 0.0
    primary.cy = 0.0
    primary.placement = "anchor"
    primary.placed = True

    buckets: DefaultDict[str, List[Comp]] = defaultdict(list)
    for comp in anchors[1:]:
        buckets[_preferred_side_for_anchor(comp, primary)].append(comp)

    for side in ("left", "right", "top", "bottom"):
        group = sorted(buckets.get(side, []), key=lambda c: c.ref)
        offsets = _spread_offsets(len(group), SIDE_STACK_STEP)
        for comp, offset in zip(group, offsets):
            if side == "left":
                comp.cx = -ANCHOR_X_GAP
                comp.cy = offset
            elif side == "right":
                comp.cx = ANCHOR_X_GAP
                comp.cy = offset
            elif side == "top":
                comp.cx = offset
                comp.cy = -ANCHOR_Y_GAP
            else:
                comp.cx = offset
                comp.cy = ANCHOR_Y_GAP
            comp.placement = "anchor"
            comp.placed = True


def _nearest_anchor_for_comp(comp: Comp, anchors: Sequence[Comp]) -> Optional[Comp]:
    if comp.anchor_ref:
        for anchor in anchors:
            if anchor.ref == comp.anchor_ref:
                return anchor

    scored: List[Tuple[int, Tuple[int, int, int, int, str], Comp]] = []
    for anchor in anchors:
        shared = len(_shared_nets(comp, anchor, include_power=False))
        if shared <= 0:
            continue
        scored.append((shared, _anchor_score(anchor), anchor))
    if scored:
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return scored[0][2]
    return anchors[0] if anchors else None


def _local_distance(comp: Comp, strength: int) -> float:
    base = 42.0
    if comp.placement == "local_power":
        base = 82.0 if comp.role == "bypass" else 98.0
    elif comp.role in {"pullup", "pulldown", "termination"}:
        base = 56.0
    elif comp.role in {"series", "filter"}:
        base = 68.0
    return base + (100 - max(1, min(100, strength))) * 0.28


def _place_near_anchor(
    comp: Comp,
    anchor: Comp,
    slot_offset: float,
    strength: int = 100,
) -> None:
    anchor_pin = _choose_anchor_pin(comp, anchor)
    comp.anchor_pin = anchor_pin
    anchor_pos = _pin_pos(anchor, anchor_pin)
    if anchor_pos is None:
        anchor_pos = (anchor.cx, anchor.cy)
    anchor_side = _pin_side(anchor, anchor_pin)

    target_pin = _target_pin_for_anchor(comp, anchor, anchor_pin)
    if _is_two_pin(comp) or _is_passive(comp):
        grounded = _grounded_cap_pins(comp)
        if grounded is not None:
            gnd_pin, _other_pin = grounded
            ground_side = "top" if anchor_side == "top" else "bottom"
            comp.rotation = _rotation_for_pin_side(comp, gnd_pin, ground_side)
        else:
            comp.rotation = _rotation_for_pin_side(comp, target_pin, _opposite(anchor_side))

    rel_pin = _pin_map(comp, 0.0, 0.0, comp.rotation).get(str(target_pin), (0.0, 0.0))
    ux, uy = _unit_for_side(anchor_side)
    tx, ty = _tangent_for_side(anchor_side)
    distance = _local_distance(comp, strength)

    desired_x = anchor_pos[0] + ux * distance + tx * slot_offset
    desired_y = anchor_pos[1] + uy * distance + ty * slot_offset
    comp.cx = desired_x - rel_pin[0]
    comp.cy = desired_y - rel_pin[1]
    comp.placed = True


def _place_free_near_anchor(comp: Comp, anchor: Comp, slot_offset: float) -> None:
    if not comp.anchor_pin:
        comp.anchor_pin = _choose_anchor_pin(comp, anchor)
    side = _pin_side(anchor, comp.anchor_pin)
    _place_near_anchor(comp, anchor, slot_offset, strength=68)
    if comp.placement == "free":
        comp.placement = f"near_{side}"


def _padded_overlap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], pad: float = 18.0) -> bool:
    return not (a[2] + pad <= b[0] or a[0] >= b[2] + pad or a[3] + pad <= b[1] or a[1] >= b[3] + pad)


def _apply_local_lanes(comps: Dict[str, Comp]) -> None:
    groups: DefaultDict[Tuple[str, str], List[Comp]] = defaultdict(list)
    for comp in comps.values():
        if not comp.anchor_ref or comp.anchor_ref not in comps or not comp.anchor_pin:
            continue
        if comp.placement not in {"local_signal", "local_power"} and not _is_local_role(comp):
            continue
        anchor = comps[comp.anchor_ref]
        side = _pin_side(anchor, comp.anchor_pin)
        groups[(comp.anchor_ref, side)].append(comp)

    for (anchor_ref, side), group in groups.items():
        if len(group) <= 1:
            continue
        lane_boxes: List[List[Tuple[float, float, float, float]]] = []
        base_pos = {comp.ref: (comp.cx, comp.cy) for comp in group}
        for comp in sorted(group, key=lambda item: (item.cy, item.cx, item.ref)):
            base_x, base_y = base_pos[comp.ref]
            chosen_lane = 0
            chosen_box: Optional[Tuple[float, float, float, float]] = None
            for lane in range(8):
                comp.cx, comp.cy = base_x, base_y
                offset = lane * 132.0
                if side == "left":
                    comp.cx -= offset
                elif side == "right":
                    comp.cx += offset
                elif side == "top":
                    comp.cy -= lane * 104.0
                else:
                    comp.cy += lane * 104.0
                box = _component_bounds(comp)
                if lane >= len(lane_boxes):
                    lane_boxes.append([])
                if not any(_padded_overlap(box, other) for other in lane_boxes[lane]):
                    chosen_lane = lane
                    chosen_box = box
                    break
            if chosen_box is None:
                chosen_lane = len(lane_boxes)
                lane_boxes.append([])
                comp.cx, comp.cy = base_x, base_y
                if side == "left":
                    comp.cx -= chosen_lane * 132.0
                elif side == "right":
                    comp.cx += chosen_lane * 132.0
                elif side == "top":
                    comp.cy -= chosen_lane * 104.0
                else:
                    comp.cy += chosen_lane * 104.0
                chosen_box = _component_bounds(comp)
            lane_boxes[chosen_lane].append(chosen_box)


def _bypass_power_net(comp: Comp) -> str:
    return next((net_name for net_name in comp.nets.values() if _is_vcc(net_name)), "")


def _place_bypass_rail_group(anchor: Comp, group: Sequence[Comp]) -> None:
    if not group:
        return

    anchor_pin = group[0].anchor_pin or _choose_anchor_pin(group[0], anchor)
    anchor_ep = _escape_endpoint(anchor, anchor_pin)
    if anchor_ep is None:
        return

    anchor_side = _pin_side(anchor, anchor_pin)
    direction = -1.0 if anchor_side == "left" else 1.0
    if anchor_side in {"top", "bottom"}:
        direction = -1.0
    rail_y = anchor_ep.ey

    for idx, comp in enumerate(group):
        comp.anchor_pin = anchor_pin
        power_pin = _first_pin_on_net(comp, _bypass_power_net(comp))
        if not power_pin:
            power_pin = _target_pin_for_anchor(comp, anchor, anchor_pin)

        comp.rotation = _rotation_for_pin_side(comp, power_pin, "top")
        rel_power = _pin_map(comp, 0.0, 0.0, comp.rotation).get(str(power_pin), (0.0, 0.0))

        rail_x = anchor_ep.ex + direction * (118.0 + idx * 128.0)
        comp.cx = rail_x - rel_power[0]
        comp.cy = rail_y + PIN_ESCAPE - rel_power[1]
        comp.placed = True


def _resolve_overlaps(comps: Dict[str, Comp]) -> None:
    ordered = sorted(comps.values(), key=lambda c: (0 if c.placement == "anchor" else 1, c.ref))

    def priority(comp: Comp) -> int:
        if comp.placement == "anchor":
            return 4
        if comp.placement == "local_power":
            return 3
        if comp.placement == "local_signal":
            return 2
        return 1

    for _ in range(14):
        moved = False
        for i, a in enumerate(ordered):
            box_a = _component_bounds(a)
            for b in ordered[i + 1:]:
                box_b = _component_bounds(b)
                if box_a[2] <= box_b[0] or box_a[0] >= box_b[2] or box_a[3] <= box_b[1] or box_a[1] >= box_b[3]:
                    continue

                mover = b if priority(b) <= priority(a) else a
                other = a if mover is b else b
                overlap_x = min(box_a[2], box_b[2]) - max(box_a[0], box_b[0])
                overlap_y = min(box_a[3], box_b[3]) - max(box_a[1], box_b[1])

                if mover.anchor_ref and mover.anchor_ref in comps:
                    anchor = comps[mover.anchor_ref]
                    dx = mover.cx - anchor.cx
                    dy = mover.cy - anchor.cy
                    if abs(dx) >= abs(dy):
                        mover.cy += (overlap_y + 22.0) * (1.0 if dy >= 0 else -1.0)
                    else:
                        mover.cx += (overlap_x + 22.0) * (1.0 if dx >= 0 else -1.0)
                elif overlap_x < overlap_y:
                    mover.cx += (overlap_x + 28.0) * (1.0 if mover.cx >= other.cx else -1.0)
                else:
                    mover.cy += (overlap_y + 28.0) * (1.0 if mover.cy >= other.cy else -1.0)
                moved = True
                break
            if moved:
                break
        if not moved:
            break


def _layout(comps: Dict[str, Comp], nets: Dict[str, Net]) -> None:
    _resolve_semantic_anchors(comps, nets)

    anchors = _anchor_candidates(comps)
    if not anchors and comps:
        anchors = [sorted(comps.values(), key=_anchor_score, reverse=True)[0]]

    for anchor in anchors:
        anchor.rotation = _default_rotation(anchor)
    _place_anchor_shell(anchors)

    anchor_refs = {c.ref for c in anchors}
    local_by_anchor: DefaultDict[Tuple[str, int, str], List[Comp]] = defaultdict(list)
    free: List[Comp] = []

    for comp in comps.values():
        if comp.ref in anchor_refs:
            continue
        anchor = _nearest_anchor_for_comp(comp, anchors)
        if anchor is not None and (comp.anchor_ref or _is_local_role(comp) or _shared_nets(comp, anchor, include_power=False)):
            comp.anchor_ref = anchor.ref
            comp.anchor_pin = _choose_anchor_pin(comp, anchor)
            key = (anchor.ref, comp.anchor_pin, comp.placement)
            local_by_anchor[key].append(comp)
        else:
            free.append(comp)

    def _local_strength(comp: Comp) -> int:
        return comp.near[0].strength if comp.near else 82

    for key, group in local_by_anchor.items():
        anchor_ref, _pin, _placement = key
        anchor = comps[anchor_ref]
        step = 62.0 if any(item.placement == "local_power" for item in group) else 42.0
        ordered_group = sorted(group, key=lambda c: (-_local_strength(c), c.ref))
        if ordered_group and all(_is_bypass(item) for item in ordered_group):
            _place_bypass_rail_group(anchor, ordered_group)
            continue
        offsets = _spread_offsets(len(ordered_group), step)
        for comp, offset in zip(ordered_group, offsets):
            strength = _local_strength(comp)
            if comp.placement in {"local_power", "local_signal"} or _is_local_role(comp):
                _place_near_anchor(comp, anchor, offset, strength)
            else:
                _place_free_near_anchor(comp, anchor, offset)

    _apply_local_lanes(comps)

    if free:
        bottom = max((_component_bounds(c)[3] for c in comps.values() if c not in free), default=0.0)
        cols = max(1, min(3, int(math.ceil(math.sqrt(len(free))))))
        for idx, comp in enumerate(sorted(free, key=lambda c: c.ref)):
            col = idx % cols
            row = idx // cols
            comp.cx = (col - (cols - 1) / 2.0) * FREE_GRID_X
            comp.cy = bottom + 90.0 + row * FREE_GRID_Y
            comp.rotation = _default_rotation(comp)
            comp.placed = True

    _resolve_overlaps(comps)

    left, top, _right, _bottom = _scene_bounds(comps.values())
    _translate(comps, MARGIN_X - left, MARGIN_Y - top)


# ---------------------------------------------------------------------------
# SVG primitives
# ---------------------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{value:.1f}"


def _line_svg(x1: float, y1: float, x2: float, y2: float, color: str = WIRE, width: float = 1.5) -> str:
    return (
        f'<line x1="{_fmt(x1)}" y1="{_fmt(y1)}" x2="{_fmt(x2)}" y2="{_fmt(y2)}" '
        f'stroke="{color}" stroke-width="{width:.1f}" stroke-linecap="round"/>'
    )


def _add_line(
    state: SvgState,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: str = WIRE,
    width: float = 1.5,
    net_name: str = "",
) -> None:
    if abs(x1 - x2) < 0.2 and abs(y1 - y2) < 0.2:
        return
    state.add(_line_svg(x1, y1, x2, y2, color, width), (min(x1, x2) - 2, min(y1, y2) - 2, max(x1, x2) + 2, max(y1, y2) + 2))
    state.add_segment(net_name, x1, y1, x2, y2)


def _dot_svg(x: float, y: float) -> str:
    return f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="3" fill="{WIRE}"/>'


def _add_dot(state: SvgState, x: float, y: float) -> None:
    state.add(_dot_svg(x, y), (x - 4, y - 4, x + 4, y + 4))


def _vcc_symbol(x: float, y: float, label: str = "VCC") -> Tuple[str, Tuple[float, float, float, float]]:
    safe = _html.escape(label)
    parts = [
        _line_svg(x, y, x, y - 14.0, POWER),
        f'<polygon points="{_fmt(x - 7)},{_fmt(y - 14)} {_fmt(x + 7)},{_fmt(y - 14)} {_fmt(x)},{_fmt(y - 24)}" fill="{POWER}"/>',
    ]
    right = x + 16.0
    if label:
        parts.append(f'<text x="{_fmt(x + 10)}" y="{_fmt(y - 12)}" fill="{REF}" font-size="10" font-family="{FONT}">{safe}</text>')
        right += len(label) * 6.0
    return "".join(parts), (x - 10, y - 28, right, y + 4)


def _gnd_symbol(x: float, y: float, label: str = "GND") -> Tuple[str, Tuple[float, float, float, float]]:
    safe = _html.escape(label)
    parts = [
        _line_svg(x, y, x, y + 9.0, POWER),
        _line_svg(x - 11, y + 9, x + 11, y + 9, POWER, 1.8),
        _line_svg(x - 7, y + 15, x + 7, y + 15, POWER, 1.4),
        _line_svg(x - 3, y + 21, x + 3, y + 21, POWER, 1.0),
    ]
    right = x + 16.0
    if label:
        parts.append(f'<text x="{_fmt(x + 13)}" y="{_fmt(y + 16)}" fill="{REF}" font-size="10" font-family="{FONT}">{safe}</text>')
        right += len(label) * 6.0
    return "".join(parts), (x - 13, y - 4, right, y + 25)


def _vcc_symbol_down(x: float, y: float, label: str = "VCC") -> Tuple[str, Tuple[float, float, float, float]]:
    safe = _html.escape(label)
    parts = [
        _line_svg(x, y, x, y + 14.0, POWER),
        f'<polygon points="{_fmt(x - 7)},{_fmt(y + 14)} {_fmt(x + 7)},{_fmt(y + 14)} {_fmt(x)},{_fmt(y + 24)}" fill="{POWER}"/>',
    ]
    right = x + 16.0
    if label:
        parts.append(f'<text x="{_fmt(x + 10)}" y="{_fmt(y + 17)}" fill="{REF}" font-size="10" font-family="{FONT}">{safe}</text>')
        right += len(label) * 6.0
    return "".join(parts), (x - 10, y - 4, right, y + 28)


def _gnd_symbol_up(x: float, y: float, label: str = "GND") -> Tuple[str, Tuple[float, float, float, float]]:
    safe = _html.escape(label)
    parts = [
        _line_svg(x, y, x, y - 9.0, POWER),
        _line_svg(x - 11, y - 9, x + 11, y - 9, POWER, 1.8),
        _line_svg(x - 7, y - 15, x + 7, y - 15, POWER, 1.4),
        _line_svg(x - 3, y - 21, x + 3, y - 21, POWER, 1.0),
    ]
    right = x + 16.0
    if label:
        parts.append(f'<text x="{_fmt(x + 13)}" y="{_fmt(y - 13)}" fill="{REF}" font-size="10" font-family="{FONT}">{safe}</text>')
        right += len(label) * 6.0
    return "".join(parts), (x - 13, y - 25, right, y + 4)


def _vcc_side_symbol(x: float, y: float, side: str, label: str) -> Tuple[str, Tuple[float, float, float, float]]:
    safe = _html.escape(label)
    direction = -1.0 if side == "left" else 1.0
    neck_x = x + direction * 14.0
    tip_x = x + direction * 24.0
    base_x = x + direction * 14.0
    label_x = x + direction * 31.0
    anchor = "end" if side == "left" else "start"
    parts = [
        _line_svg(x, y, neck_x, y, POWER),
        (
            f'<polygon points="{_fmt(base_x)},{_fmt(y - 7)} '
            f'{_fmt(base_x)},{_fmt(y + 7)} {_fmt(tip_x)},{_fmt(y)}" fill="{POWER}"/>'
        ),
        (
            f'<text x="{_fmt(label_x)}" y="{_fmt(y)}" fill="{REF}" font-size="10" font-family="{FONT}" '
            f'text-anchor="{anchor}" dominant-baseline="middle">{safe}</text>'
        ),
    ]
    text_w = len(label) * 6.0 + 4.0
    if side == "left":
        return "".join(parts), (label_x - text_w, y - 11, x + 3, y + 11)
    return "".join(parts), (x - 3, y - 11, label_x + text_w, y + 11)


def _gnd_side_symbol(x: float, y: float, side: str, label: str) -> Tuple[str, Tuple[float, float, float, float]]:
    safe = _html.escape(label)
    direction = -1.0 if side == "left" else 1.0
    stem_x = x + direction * 9.0
    bar1_x = stem_x
    bar2_x = stem_x + direction * 6.0
    bar3_x = stem_x + direction * 12.0
    label_x = stem_x + direction * 20.0
    anchor = "end" if side == "left" else "start"
    parts = [
        _line_svg(x, y, stem_x, y, POWER),
        _line_svg(bar1_x, y - 11, bar1_x, y + 11, POWER, 1.8),
        _line_svg(bar2_x, y - 7, bar2_x, y + 7, POWER, 1.4),
        _line_svg(bar3_x, y - 3, bar3_x, y + 3, POWER, 1.0),
        (
            f'<text x="{_fmt(label_x)}" y="{_fmt(y)}" fill="{REF}" font-size="10" font-family="{FONT}" '
            f'text-anchor="{anchor}" dominant-baseline="middle">{safe}</text>'
        ),
    ]
    text_w = len(label) * 6.0 + 4.0
    if side == "left":
        return "".join(parts), (label_x - text_w, y - 13, x + 3, y + 13)
    return "".join(parts), (x - 3, y - 13, label_x + text_w, y + 13)


def _net_label_svg(x: float, y: float, text: str, anchor: str = "start") -> str:
    return (
        f'<text x="{_fmt(x)}" y="{_fmt(y)}" fill="{LABEL}" font-size="11" font-family="{FONT}" '
        f'text-anchor="{anchor}" dominant-baseline="middle">{_html.escape(text)}</text>'
    )


def _label_box(text: str, x: float, y: float, anchor: str = "start") -> Tuple[float, float, float, float]:
    width = max(28.0, len(text) * 6.7 + 8.0)
    if anchor == "end":
        return x - width, y - 8.0, x, y + 8.0
    if anchor == "middle":
        return x - width / 2.0, y - 8.0, x + width / 2.0, y + 8.0
    return x, y - 8.0, x + width, y + 8.0


def _overlap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def _place_label(
    state: SvgState,
    text: str,
    candidates: Sequence[Tuple[float, float, str]],
    occupied: List[Tuple[float, float, float, float]],
) -> None:
    if not text:
        return
    choice = candidates[0]
    for cand in candidates:
        box = _label_box(text, cand[0], cand[1], cand[2])
        if not any(_overlap(box, item) for item in occupied):
            choice = cand
            break
    x, y, anchor = choice
    box = _label_box(text, x, y, anchor)
    occupied.append(box)
    state.add(_net_label_svg(x, y, text, anchor), box)


def _component_label(cx: float, cy: float, ref: str, value: str) -> str:
    return (
        f'<text x="{_fmt(cx + 17)}" y="{_fmt(cy - 5)}" fill="{REF}" font-size="11" font-family="{FONT}" text-anchor="start">{_html.escape(ref)}</text>'
        f'<text x="{_fmt(cx + 17)}" y="{_fmt(cy + 11)}" fill="{VAL}" font-size="10" font-family="{FONT}" text-anchor="start">{_html.escape(value)}</text>'
    )


def _component_labels_for(comp: Comp) -> str:
    if _is_two_pin(comp) and _is_passive(comp):
        pins = list(_pin_map(comp).values())
        if len(pins) >= 2:
            dx = abs(pins[0][0] - pins[1][0])
            dy = abs(pins[0][1] - pins[1][1])
            if dx >= dy:
                return (
                    f'<text x="{_fmt(comp.cx)}" y="{_fmt(comp.cy - 25)}" fill="{REF}" font-size="11" font-family="{FONT}" text-anchor="middle">{_html.escape(comp.ref)}</text>'
                    f'<text x="{_fmt(comp.cx)}" y="{_fmt(comp.cy + 25)}" fill="{VAL}" font-size="10" font-family="{FONT}" text-anchor="middle">{_html.escape(comp.value)}</text>'
                )
            return (
                f'<text x="{_fmt(comp.cx + 24)}" y="{_fmt(comp.cy - 8)}" fill="{REF}" font-size="11" font-family="{FONT}" text-anchor="start">{_html.escape(comp.ref)}</text>'
                f'<text x="{_fmt(comp.cx + 24)}" y="{_fmt(comp.cy + 7)}" fill="{VAL}" font-size="10" font-family="{FONT}" text-anchor="start">{_html.escape(comp.value)}</text>'
            )
    return _component_label(comp.cx, comp.cy, comp.ref, comp.value)


def _grid(width: float, height: float) -> str:
    return (
        '<defs><pattern id="kgrid" x="0" y="0" width="10" height="10" patternUnits="userSpaceOnUse">'
        f'<path d="M 10 0 L 0 0 0 10" fill="none" stroke="{GRID}" stroke-width="0.4"/>'
        '</pattern></defs>'
        f'<rect width="{_fmt(width)}" height="{_fmt(height)}" fill="url(#kgrid)"/>'
    )


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def _escape_endpoint(comp: Comp, pin: int) -> Optional[Endpoint]:
    pos = _pin_pos(comp, pin)
    if pos is None:
        return None
    side = _pin_side(comp, pin)
    ux, uy = _unit_for_side(side)
    ex = pos[0] + ux * PIN_ESCAPE
    ey = pos[1] + uy * PIN_ESCAPE
    return Endpoint(comp=comp, pin=pin, x=pos[0], y=pos[1], side=side, ex=ex, ey=ey)


def _endpoint_anchor(ep: Endpoint) -> str:
    if ep.side == "left":
        return "end"
    if ep.side == "right":
        return "start"
    return "start"


def _clean_points(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for x, y in points:
        if out and abs(out[-1][0] - x) < 0.2 and abs(out[-1][1] - y) < 0.2:
            continue
        out.append((x, y))
        while len(out) >= 3:
            a = out[-3]
            b = out[-2]
            c = out[-1]
            same_x = abs(a[0] - b[0]) < 0.2 and abs(b[0] - c[0]) < 0.2
            same_y = abs(a[1] - b[1]) < 0.2 and abs(b[1] - c[1]) < 0.2
            if not (same_x or same_y):
                break
            out.pop(-2)
    return out


def _path_has_u_turn(points: Sequence[Tuple[float, float]]) -> bool:
    pts = list(points)
    if len(pts) < 3:
        return False
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        dx1 = b[0] - a[0]
        dy1 = b[1] - a[1]
        dx2 = c[0] - b[0]
        dy2 = c[1] - b[1]
        if abs(dx1) < 0.2 and abs(dx2) < 0.2 and dy1 * dy2 < -0.2:
            return True
        if abs(dy1) < 0.2 and abs(dy2) < 0.2 and dx1 * dx2 < -0.2:
            return True
    return False


def _draw_path(state: SvgState, points: Sequence[Tuple[float, float]], net_name: str = "") -> None:
    pts = _clean_points(points)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        _add_line(state, x1, y1, x2, y2, net_name=net_name)


def _draw_escape(state: SvgState, ep: Endpoint, net_name: str = "") -> None:
    _add_line(state, ep.x, ep.y, ep.ex, ep.ey, net_name=net_name)


def _segment_intersection_point(
    a: Tuple[str, float, float, float, float],
    b: Tuple[str, float, float, float, float],
) -> Optional[Tuple[float, float]]:
    _, ax1, ay1, ax2, ay2 = a
    _, bx1, by1, bx2, by2 = b
    a_vert = abs(ax1 - ax2) < 0.5
    a_horz = abs(ay1 - ay2) < 0.5
    b_vert = abs(bx1 - bx2) < 0.5
    b_horz = abs(by1 - by2) < 0.5

    if a_vert and b_horz:
        x, y = ax1, by1
        if min(bx1, bx2) - 0.5 <= x <= max(bx1, bx2) + 0.5 and min(ay1, ay2) - 0.5 <= y <= max(ay1, ay2) + 0.5:
            return x, y
    elif a_horz and b_vert:
        x, y = bx1, ay1
        if min(ax1, ax2) - 0.5 <= x <= max(ax1, ax2) + 0.5 and min(by1, by2) - 0.5 <= y <= max(by1, by2) + 0.5:
            return x, y
    elif a_vert and b_vert and abs(ax1 - bx1) < 0.5:
        top = max(min(ay1, ay2), min(by1, by2))
        bottom = min(max(ay1, ay2), max(by1, by2))
        if bottom >= top - 0.5:
            return ax1, top
    elif a_horz and b_horz and abs(ay1 - by1) < 0.5:
        left = max(min(ax1, ax2), min(bx1, bx2))
        right = min(max(ax1, ax2), max(bx1, bx2))
        if right >= left - 0.5:
            return left, ay1
    return None


def _add_net_junction_dots(state: SvgState, net_name: str) -> None:
    segments = [(n, x1, y1, x2, y2) for n, x1, y1, x2, y2 in state.segments if n == net_name]
    if len(segments) < 2:
        return

    points: Set[Tuple[float, float]] = set()
    for _, x1, y1, x2, y2 in segments:
        points.add((round(x1, 1), round(y1, 1)))
        points.add((round(x2, 1), round(y2, 1)))

    for idx, a in enumerate(segments):
        for b in segments[idx + 1 :]:
            point = _segment_intersection_point(a, b)
            if point is not None:
                points.add((round(point[0], 1), round(point[1], 1)))

    seen: Set[Tuple[float, float]] = set()
    for x, y in points:
        if (x, y) in seen:
            continue
        degree = 0
        for _, x1, y1, x2, y2 in segments:
            if _point_on_segment(x, y, x1, y1, x2, y2):
                is_endpoint = (
                    (abs(x - x1) < 0.5 and abs(y - y1) < 0.5)
                    or (abs(x - x2) < 0.5 and abs(y - y2) < 0.5)
                )
                degree += 1 if is_endpoint else 2
        if degree >= 3:
            _add_dot(state, x, y)
        seen.add((x, y))


def _segment_hits_box(x1: float, y1: float, x2: float, y2: float, box: Tuple[float, float, float, float], pad: float = 24.0) -> bool:
    left, top, right, bottom = box
    left -= pad
    top -= pad
    right += pad
    bottom += pad
    if abs(x1 - x2) < 0.5:
        x = x1
        if x < left or x > right:
            return False
        return not (max(y1, y2) < top or min(y1, y2) > bottom)
    if abs(y1 - y2) < 0.5:
        y = y1
        if y < top or y > bottom:
            return False
        return not (max(x1, x2) < left or min(x1, x2) > right)
    return True


def _overlap_amount(a1: float, a2: float, b1: float, b2: float) -> float:
    return min(max(a1, a2), max(b1, b2)) - max(min(a1, a2), min(b1, b2))


def _range_touches(a1: float, a2: float, b1: float, b2: float) -> bool:
    return _overlap_amount(a1, a2, b1, b2) >= -0.5


def _point_on_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    if abs(x1 - x2) < 0.5:
        return abs(px - x1) < 0.5 and min(y1, y2) - 0.5 <= py <= max(y1, y2) + 0.5
    if abs(y1 - y2) < 0.5:
        return abs(py - y1) < 0.5 and min(x1, x2) - 0.5 <= px <= max(x1, x2) + 0.5
    return False


def _segments_touch_or_cross(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    a_vert = abs(ax1 - ax2) < 0.5
    b_vert = abs(bx1 - bx2) < 0.5
    a_horz = abs(ay1 - ay2) < 0.5
    b_horz = abs(by1 - by2) < 0.5

    if a_vert and b_vert:
        return abs(ax1 - bx1) < 0.5 and _range_touches(ay1, ay2, by1, by2)
    if a_horz and b_horz:
        return abs(ay1 - by1) < 0.5 and _range_touches(ax1, ax2, bx1, bx2)
    if a_vert and b_horz:
        return _point_on_segment(ax1, by1, ax1, ay1, ax2, ay2) and _point_on_segment(ax1, by1, bx1, by1, bx2, by2)
    if a_horz and b_vert:
        return _point_on_segment(bx1, ay1, ax1, ay1, ax2, ay2) and _point_on_segment(bx1, ay1, bx1, by1, bx2, by2)
    return True


def _path_conflicts_existing(points: Sequence[Tuple[float, float]], state: SvgState, net_name: str) -> bool:
    pts = _clean_points(points)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        for other_net, ox1, oy1, ox2, oy2 in state.segments:
            if other_net == net_name:
                continue
            if _segments_touch_or_cross((x1, y1, x2, y2), (ox1, oy1, ox2, oy2)):
                return True
    return False


def _path_clear(
    points: Sequence[Tuple[float, float]],
    comps: Dict[str, Comp],
    ignore: Set[str],
    state: Optional[SvgState] = None,
    net_name: str = "",
) -> bool:
    if _path_has_u_turn(points):
        return False
    pts = _clean_points(points)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        for comp in comps.values():
            if comp.ref in ignore:
                continue
            if _segment_hits_box(x1, y1, x2, y2, _component_bounds(comp)):
                return False
    if state is not None and net_name and _path_conflicts_existing(pts, state, net_name):
        return False
    return True


def _union_bounds(comps: Sequence[Comp]) -> Tuple[float, float, float, float]:
    boxes = [_component_bounds(c) for c in comps]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _path_label_candidates(path: Sequence[Tuple[float, float]]) -> List[Tuple[float, float, str]]:
    pts = _clean_points(path)
    if len(pts) < 2:
        return [(0.0, 0.0, "start")]
    best = (pts[0], pts[1])
    best_len = -1.0
    for a, b in zip(pts, pts[1:]):
        length = abs(a[0] - b[0]) + abs(a[1] - b[1])
        if length > best_len:
            best = (a, b)
            best_len = length
    (x1, y1), (x2, y2) = best
    if abs(y1 - y2) < 0.5:
        x = (x1 + x2) / 2.0
        y = y1 - 13.0
        return [(x, y, "middle"), (x, y + 26.0, "middle"), (x + 10.0, y, "start")]
    x = x1 + 14.0
    y = (y1 + y2) / 2.0
    return [(x, y, "start"), (x - 28.0, y, "end"), (x, y - 16.0, "start")]


def _path_length(points: Sequence[Tuple[float, float]]) -> float:
    pts = _clean_points(points)
    return sum(abs(x1 - x2) + abs(y1 - y2) for (x1, y1), (x2, y2) in zip(pts, pts[1:]))


def _endpoint_label_candidates(ep: Endpoint) -> List[Tuple[float, float, str]]:
    anchor = _endpoint_anchor(ep)
    dx = -12.0 if anchor == "end" else 12.0
    return [
        (ep.ex + dx, ep.ey, anchor),
        (ep.ex + dx, ep.ey - 16.0, anchor),
        (ep.ex + dx, ep.ey + 16.0, anchor),
    ]


def _pair_route_candidates(a: Endpoint, b: Endpoint, comps: Dict[str, Comp]) -> List[List[Tuple[float, float]]]:
    p1 = (a.ex, a.ey)
    p2 = (b.ex, b.ey)
    candidates: List[List[Tuple[float, float]]] = []

    if abs(a.ey - b.ey) < 0.5 or abs(a.ex - b.ex) < 0.5:
        candidates.append([p1, p2])
    candidates.append([p1, (a.ex, b.ey), p2])
    candidates.append([p1, (b.ex, a.ey), p2])

    left, top, right, bottom = _union_bounds([a.comp, b.comp])
    for idx in range(12):
        lane = 28.0 + idx * WIRE_LANE
        candidates.append([p1, (left - lane, a.ey), (left - lane, b.ey), p2])
        candidates.append([p1, (right + lane, a.ey), (right + lane, b.ey), p2])
        candidates.append([p1, (a.ex, top - lane), (b.ex, top - lane), p2])
        candidates.append([p1, (a.ex, bottom + lane), (b.ex, bottom + lane), p2])
    return candidates


def _draw_pair_route(
    state: SvgState,
    net: Net,
    a: Endpoint,
    b: Endpoint,
    comps: Dict[str, Comp],
    occupied_labels: List[Tuple[float, float, float, float]],
) -> None:
    ignore = {a.comp.ref, b.comp.ref}
    chosen: Optional[List[Tuple[float, float]]] = None
    for path in _pair_route_candidates(a, b, comps):
        if _path_clear(path, comps, ignore, state, net.name):
            chosen = path
            break
    if chosen is None:
        chosen = _pair_route_candidates(a, b, comps)[-1]

    direct_len = abs(a.ex - b.ex) + abs(a.ey - b.ey)
    chosen_len = _path_length(chosen)
    local_pair = bool(a.comp.anchor_ref == b.comp.ref or b.comp.anchor_ref == a.comp.ref or a.comp.placement.startswith("local") or b.comp.placement.startswith("local"))
    if chosen_len > max(520.0, direct_len * 2.6) or (local_pair and direct_len > 420.0):
        label = net.display or net.name
        _place_label(state, label, _endpoint_label_candidates(a), occupied_labels)
        _place_label(state, label, _endpoint_label_candidates(b), occupied_labels)
        return

    _draw_path(state, chosen, net.name)
    if net.has_label or net.display != net.name:
        _place_label(state, net.display or net.name, _path_label_candidates(chosen), occupied_labels)


def _draw_spine_route(
    state: SvgState,
    net: Net,
    endpoints: Sequence[Endpoint],
    comps: Dict[str, Comp],
    occupied_labels: List[Tuple[float, float, float, float]],
) -> None:
    comp_refs = {ep.comp.ref for ep in endpoints}
    left, top, right, bottom = _union_bounds([ep.comp for ep in endpoints])
    ys = [ep.ey for ep in endpoints]
    candidate_xs: List[float] = []
    for idx in range(14):
        lane = 32.0 + idx * WIRE_LANE
        candidate_xs.extend([left - lane, right + lane])

    chosen_x = candidate_xs[0]
    for x in candidate_xs:
        tests = [[(x, min(ys)), (x, max(ys))]]
        tests.extend([[(ep.ex, ep.ey), (x, ep.ey)] for ep in endpoints])
        if all(_path_clear(path, comps, comp_refs, state, net.name) for path in tests):
            chosen_x = x
            break

    if max(ys) - min(ys) > 0.5:
        _draw_path(state, [(chosen_x, min(ys)), (chosen_x, max(ys))], net.name)
    for ep in endpoints:
        _draw_path(state, [(ep.ex, ep.ey), (chosen_x, ep.ey)], net.name)

    if net.has_label or net.display != net.name:
        anchor = "end" if chosen_x < (left + right) / 2.0 else "start"
        label_x = chosen_x - 14.0 if anchor == "end" else chosen_x + 14.0
        label_y = min(max((top + bottom) / 2.0, min(ys)), max(ys))
        _place_label(
            state,
            net.display or net.name,
            [(label_x, label_y, anchor), (label_x, label_y - 18.0, anchor), (label_x, label_y + 18.0, anchor)],
            occupied_labels,
        )


def _collect_endpoints(comps: Dict[str, Comp], net_name: str) -> List[Endpoint]:
    endpoints: List[Endpoint] = []
    for comp in comps.values():
        for pin, nn in comp.nets.items():
            if nn != net_name:
                continue
            ep = _escape_endpoint(comp, pin)
            if ep is not None:
                endpoints.append(ep)
    return endpoints


def _draw_power_symbol_at(state: SvgState, net: Net, x: float, y: float, seen: Set[str]) -> None:
    label = net.display or net.name
    if _is_vcc(net.name):
        svg, bounds = _vcc_symbol(x, y, label)
    else:
        svg, bounds = _gnd_symbol(x, y, label)
    state.add(svg, bounds)
    seen.add(net.name)


def _draw_power_symbol_from_node(
    state: SvgState,
    net: Net,
    x: float,
    y: float,
    seen: Set[str],
    direct_vertical: bool = False,
) -> None:
    if direct_vertical:
        _draw_power_symbol_at(state, net, x, y, seen)
        return

    symbol_y = y - POWER_SYMBOL_LEAD if _is_vcc(net.name) else y + POWER_SYMBOL_LEAD
    _draw_path(state, [(x, y), (x, symbol_y)], net.name)
    _draw_power_symbol_at(state, net, x, symbol_y, seen)


def _draw_power_label_from_endpoint(state: SvgState, net: Net, ep: Endpoint) -> None:
    label = net.display or net.name
    if _is_vcc(net.name):
        svg, bounds = _vcc_side_symbol(ep.ex, ep.ey, ep.side, label)
    else:
        svg, bounds = _gnd_side_symbol(ep.ex, ep.ey, ep.side, label)
    state.add(svg, bounds)


def _draw_power_symbol_from_endpoint(state: SvgState, net: Net, ep: Endpoint, seen: Set[str]) -> None:
    if ep.side in {"left", "right"}:
        _draw_power_label_from_endpoint(state, net, ep)
        return
    label = net.display or net.name
    if _is_vcc(net.name):
        svg, bounds = _vcc_symbol(ep.ex, ep.ey, label) if ep.side == "top" else _vcc_symbol_down(ep.ex, ep.ey, label)
    else:
        svg, bounds = _gnd_symbol_up(ep.ex, ep.ey, label) if ep.side == "top" else _gnd_symbol(ep.ex, ep.ey, label)
    state.add(svg, bounds)
    seen.add(net.name)


def _draw_local_power_clusters(
    state: SvgState,
    comps: Dict[str, Comp],
    nets: Dict[str, Net],
    handled: Set[Tuple[str, int]],
    seen_power_labels: Set[str],
) -> None:
    groups: DefaultDict[Tuple[str, int, str], List[Comp]] = defaultdict(list)
    for comp in sorted(comps.values(), key=lambda c: c.ref):
        if not _is_bypass(comp) or not comp.anchor_ref or comp.anchor_ref not in comps:
            continue
        anchor = comps[comp.anchor_ref]
        power_net = next((n for n in comp.nets.values() if _is_vcc(n) and n in anchor.nets.values()), "")
        if not power_net:
            continue
        anchor_pin = comp.anchor_pin or _first_pin_on_net(anchor, power_net)
        if not anchor_pin:
            continue
        groups[(anchor.ref, anchor_pin, power_net)].append(comp)

    for (anchor_ref, anchor_pin, power_net), group in groups.items():
        if anchor_ref not in comps:
            continue
        anchor = comps[anchor_ref]
        anchor_ep = _escape_endpoint(anchor, anchor_pin)
        if anchor_ep is None:
            continue

        cap_points: List[Endpoint] = []
        for comp in sorted(group, key=lambda c: c.cx):
            cap_pin = _first_pin_on_net(comp, power_net)
            if not cap_pin:
                continue
            cap_ep = _escape_endpoint(comp, cap_pin)
            if cap_ep is None:
                continue
            cap_points.append(cap_ep)

        if not cap_points:
            continue

        xs = [anchor_ep.ex] + [ep.ex for ep in cap_points]
        direction = -1.0 if sum(ep.ex for ep in cap_points) / len(cap_points) < anchor_ep.ex else 1.0
        ignore = {anchor.ref}
        ignore.update(ep.comp.ref for ep in cap_points)

        rail_candidates = [anchor_ep.ey]
        for idx in range(1, 7):
            rail_candidates.extend([anchor_ep.ey + idx * WIRE_LANE, anchor_ep.ey - idx * WIRE_LANE])

        rail_y = rail_candidates[0]
        rail_end_x = (min(xs) - 38.0) if direction < 0 else (max(xs) + 38.0)
        for candidate_y in rail_candidates:
            candidate_end_x = (min(xs) - 38.0) if direction < 0 else (max(xs) + 38.0)
            candidate_paths = [[(anchor_ep.ex, candidate_y), (candidate_end_x, candidate_y)]]
            candidate_paths.extend(
                [[(ep.ex, ep.ey), (ep.ex, candidate_y)]]
                for ep in cap_points
                if abs(ep.ey - candidate_y) > 0.5
            )
            anchor_leg = [(anchor_ep.ex, anchor_ep.ey), (anchor_ep.ex, candidate_y)]
            if abs(anchor_ep.ey - candidate_y) > 0.5:
                candidate_paths.append(anchor_leg)
            if all(_path_clear(path, comps, ignore, state, power_net) for path in candidate_paths):
                rail_y = candidate_y
                rail_end_x = candidate_end_x
                break

        _draw_escape(state, anchor_ep, power_net)
        if abs(anchor_ep.ey - rail_y) > 0.5:
            _draw_path(state, [(anchor_ep.ex, anchor_ep.ey), (anchor_ep.ex, rail_y)], power_net)
        bus_path = [(anchor_ep.ex, rail_y), (rail_end_x, rail_y)]
        _draw_path(state, bus_path, power_net)

        for ep in cap_points:
            _draw_escape(state, ep, power_net)
            if abs(ep.ey - rail_y) > 0.5:
                _draw_path(state, [(ep.ex, ep.ey), (ep.ex, rail_y)], power_net)
            handled.add((ep.comp.ref, ep.pin))

        net = nets.get(power_net, Net(power_net, power_net, "power"))
        _draw_power_symbol_from_node(state, net, rail_end_x, rail_y, seen_power_labels)
        handled.add((anchor.ref, anchor_pin))


def _route_power_net(
    state: SvgState,
    net: Net,
    endpoints: Sequence[Endpoint],
    comps: Dict[str, Comp],
    handled: Set[Tuple[str, int]],
    seen_power_labels: Set[str],
) -> None:
    singles: List[Endpoint] = []

    for ep in endpoints:
        if (ep.comp.ref, ep.pin) in handled:
            continue
        singles.append(ep)

    for ep in singles:
        _draw_escape(state, ep, net.name)
        _draw_power_symbol_from_endpoint(state, net, ep, seen_power_labels)


def _route_signal_net(
    state: SvgState,
    net: Net,
    endpoints: Sequence[Endpoint],
    comps: Dict[str, Comp],
    occupied_labels: List[Tuple[float, float, float, float]],
) -> None:
    if not endpoints:
        return

    if _detect_bus_like(net) or len(endpoints) >= 5:
        for ep in endpoints:
            _draw_escape(state, ep, net.name)
            anchor = _endpoint_anchor(ep)
            dx = -12.0 if anchor == "end" else 12.0
            candidates = [
                (ep.ex + dx, ep.ey, anchor),
                (ep.ex + dx, ep.ey - 16.0, anchor),
                (ep.ex + dx, ep.ey + 16.0, anchor),
            ]
            _place_label(state, net.display or net.name, candidates, occupied_labels)
        return

    if len(endpoints) == 1:
        ep = endpoints[0]
        if net.has_label or net.display != net.name:
            _draw_escape(state, ep, net.name)
            _place_label(state, net.display or net.name, _endpoint_label_candidates(ep), occupied_labels)
        return

    for ep in endpoints:
        _draw_escape(state, ep, net.name)

    if len(endpoints) == 2:
        _draw_pair_route(state, net, endpoints[0], endpoints[1], comps, occupied_labels)
        return

    _draw_spine_route(state, net, endpoints, comps, occupied_labels)


def _route(comps: Dict[str, Comp], nets: Dict[str, Net]) -> SvgState:
    state = SvgState()
    occupied_labels: List[Tuple[float, float, float, float]] = [_component_bounds(c) for c in comps.values()]
    handled_power: Set[Tuple[str, int]] = set()
    seen_power_labels: Set[str] = set()

    _draw_local_power_clusters(state, comps, nets, handled_power, seen_power_labels)

    for net_name, net in nets.items():
        endpoints = _collect_endpoints(comps, net_name)
        if not endpoints:
            continue
        if _is_power(net_name):
            _route_power_net(state, net, endpoints, comps, handled_power, seen_power_labels)
        else:
            _route_signal_net(state, net, endpoints, comps, occupied_labels)
        _add_net_junction_dots(state, net_name)

    return state


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _component_svg(comp: Comp) -> Tuple[str, Tuple[float, float, float, float]]:
    n_pins = max(comp.nets.keys()) if comp.nets else 2

    group = render_symbol_group(comp.lib, comp.sym, comp.cx, comp.cy, comp.rotation, SYM_SCALE)
    is_box = False
    if not group or group.strip() in {"", "<g>\n  \n</g>", "<g></g>"}:
        custom = load_custom_symbol(comp.sym)
        if custom and custom.get("pins"):
            group = render_custom_ic(comp.sym, custom["pins"], comp.cx, comp.cy)
            is_box = True
            n_pins = max((int(k) for k in custom["pins"].keys() if str(k).isdigit()), default=n_pins)
        else:
            group = render_generic_ic(comp.sym, n_pins, comp.cx, comp.cy)
            is_box = True

    if is_box:
        box_h = (math.ceil(n_pins / 2) + 1) * 26.0
        label_y = comp.cy + box_h / 2.0 + 10.0
        labels = (
            f'<text x="{_fmt(comp.cx)}" y="{_fmt(label_y)}" fill="{REF}" font-size="11" font-family="{FONT}" text-anchor="middle">{_html.escape(comp.ref)}</text>'
            f'<text x="{_fmt(comp.cx)}" y="{_fmt(label_y + 13)}" fill="{VAL}" font-size="10" font-family="{FONT}" text-anchor="middle">{_html.escape(comp.value)}</text>'
        )
    else:
        labels = _component_labels_for(comp)

    return group + labels, _component_bounds(comp)


def render_netlist(text: str) -> str:
    try:
        comps, nets = _parse(text)
        if not comps:
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="280" height="60">'
                f'<rect width="100%" height="100%" fill="{BG}"/>'
                f'<text x="14" y="34" fill="{VAL}" font-size="12" font-family="{FONT}">No components parsed.</text>'
                "</svg>"
            )

        _layout(comps, nets)
        routes = _route(comps, nets)

        component_parts: List[str] = []
        all_bounds: List[Tuple[float, float, float, float]] = list(routes.bounds)
        for comp in comps.values():
            svg, bounds = _component_svg(comp)
            component_parts.append(svg)
            all_bounds.append(bounds)

        if all_bounds:
            right = max(b[2] for b in all_bounds)
            bottom = max(b[3] for b in all_bounds)
        else:
            right = 320.0
            bottom = 220.0

        canvas_w = max(280.0, right + MARGIN_X + CANVAS_PAD_X)
        canvas_h = max(160.0, bottom + MARGIN_Y + CANVAS_PAD_Y)

        parts: List[str] = [
            f'<rect width="100%" height="100%" fill="{BG}"/>',
            _grid(canvas_w, canvas_h),
        ]
        parts.extend(routes.parts)
        parts.extend(component_parts)

        body = "\n  ".join(parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w:.0f}" height="{canvas_h:.0f}" '
            f'viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}">\n'
            f"  {body}\n"
            "</svg>"
        )
    except Exception as exc:
        return _error_svg(str(exc))


def save_custom_component_def(name: str, data: dict) -> None:
    save_custom_symbol(name, data)


def _error_svg(message: str) -> str:
    safe = _html.escape(message or "Unknown schematic rendering error.")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="120">'
        f'<rect width="100%" height="100%" fill="{BG}"/>'
        f'<rect x="10" y="10" width="620" height="100" rx="8" fill="#fff6f6" stroke="#840000"/>'
        f'<text x="24" y="42" fill="#840000" font-size="14" font-family="{FONT}">schematic_engine error</text>'
        f'<text x="24" y="68" fill="{VAL}" font-size="11" font-family="{FONT}">{safe}</text>'
        "</svg>"
    )
