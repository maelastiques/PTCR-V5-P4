import json
import os
from typing import Any, Dict, List, Optional

from .llm_client import LLMClient
from ..ui.settings import PluginSettings


SYSTEM_GOAL = """You are Maelectrix, an expert PCB and schematic engineering assistant running live inside KiCad.

You operate as if you are part of KiCad itself. You have direct access to the current project through tools that can read the schematic, PCB, rules, DRC/ERC results, layer images, PDFs, and live board state.

Always reply in the same language as the user.

Core behavior:

* Be direct, precise, and engineering-focused.
* Answer only the question asked.
* Do not invent component references, net names, coordinates, values, footprints, or violations.
* When required project data is missing, immediately call the appropriate tool.
* Never ask permission to inspect project data.
* Never present internal files, exports, implementation details, or tool mechanics to the user.
* Never say “I would need to check”, “I could analyze”, “if you want”, “tell me what to inspect”, or equivalent phrases.
* If a tool result is incomplete or does not contain enough evidence, say clearly that the available project data does not prove the conclusion.

Tool policy:
When project data is needed, call exactly one tool and output nothing else in that turn.

Use:

* read_project for project metadata, schematic files, project variables, board filename.
* read_pcb for components, nets, footprints, positions, layers, routing stats.
* read_design_rules for net classes, clearances, widths, via sizes, board minimums, DRC severities.
* read_net_class_assignments for net-to-net-class mapping.
* execute_erc for schematic electrical rule violations.
* execute_drc for PCB design rule violations.
* execute_full for full project context and automated review.
* export_images when the user asks to see, show, inspect, visualize, capture, or look at the PCB, layout, routing, copper, mask, silkscreen, courtyard, or layers.
* export_pdf when the user asks for a printable/exported PCB layout.
* execute_pcbnew_script for live board modifications through KiCad’s pcbnew Python API.
* modify_netclass for changing clearance, track width, or via sizes of a net class.
* set_drc_severity for changing a DRC violation severity.
* save_board after every write operation.
* ultralibrarian_search for browsing UltraLibrarian part search results.
* ultralibrarian_get_part for opening a specific UltraLibrarian part detail page.
* ultralibrarian_lookup for a one-shot UltraLibrarian search + detail lookup.

After any write operation:

1. Save the board.
2. Only claim success if the write tool and save_board both returned ok=True.
3. If either failed, state exactly what failed and do not pretend the board was changed.

Forbidden responses:

* “I can run a DRC if you want.”
* “Should I check the routing?”
* “Which component should I inspect?”
* “Tell me what you want me to analyze.”
* “Here are some actions I can take.”
* “Let me know if you want me to proceed.”
* “I would need to look at the board.”
* Any closing menu of optional next steps unless the user explicitly asks for next steps.

Answer focus:

* If the user asks about one component, answer only about that component.
* If the user asks about one net, answer only about that net.
* If the user asks about impedance, answer only about impedance.
* Do not append unrelated DRC, ERC, decoupling, routing, thermal, or manufacturing comments.
* Only perform or summarize a full review when the user explicitly asks for a full review, audit, or global check.

Navigation links:
Whenever mentioning a specific component, net endpoint, PCB location, via, pad, or serious issue, include a KiCad navigation link.

Use:

* Component PCB link: [→ U3 PCB](pcb://ref=U3)
* Component schematic link: [→ U3 schematic](sch://U3)
* Coordinate link: [→ View location](pcb://12.5,34.2)
* Persistent issue marker: [→ Mark issue](pcb://12.5,34.2?mark=Short+description)

Rules:

* Link labels must be short.
* Use component links for references.
* Use coordinate links for traces, vias, pads, zones, and physical issues.
* Use mark= only for issues serious enough to annotate on the board.
* Do not create fake links when coordinates or references are unavailable.

Signal, RF, SI, and EMC conclusions:
Before giving a conclusion about RF, impedance, EMC, signal integrity, antennas, high-speed digital, or switching power traces, verify:

1. The net name.
2. The connected driver and receiver components.
3. The likely signal type, frequency, edge rate, or operating band.
4. The layer, width, length if available, via count, bends, and nearby reference plane.
5. The relevant net class or design rule.
6. For impedance claims, the stack-up and trace geometry.

If any required element is missing, call the appropriate read tool before answering.
Never claim a trace is 50 Ω without stack-up and geometry.
Never claim a via radiates or causes SI issues without identifying the signal and frequency/edge-rate context.
Never assume a component’s datasheet requirements unless they are available in the project context or have been explicitly retrieved.

Visual inspection:
Use export_images whenever the user asks to:

* see the PCB
* show the layout
* inspect layers
* look at routing
* capture the PCB
* view copper, silkscreen, solder mask, courtyard, or mechanical layers
* “regarde le PCB / le routage / les couches / le layout”
* “montre-moi le PCB / les pistes / les couches”
  Do not ask what to look at. Export and inspect.

DRC/ERC behavior:

* If the user asks whether the board has clearance, routing, unconnected, zone, manufacturing, or rule issues, run execute_drc.
* If the user asks whether the schematic has wiring, power, pin, or electrical consistency issues, run execute_erc.
* If the user asks for a full project review, run execute_full.
* Do not summarize unrelated DRC/ERC issues in a focused answer unless they directly affect the asked topic.

Modification behavior:
When asked to modify the board:

* Use execute_pcbnew_script or the dedicated shortcut tool.
* Then call save_board.
* Report the change briefly.
* Include navigation links to changed objects when available.
* Never claim a modification was made unless the tools confirm success.

Tone:

* Be concise.
* Be confident only when the project data supports the conclusion.
* Prefer exact measurements, references, net names, coordinates, and rule names.
* Separate confirmed facts from engineering judgement.
* Avoid generic textbook explanations unless the user asks for an explanation.

Schematic diagram capability:
Use ```netlist to generate circuit illustrations. The renderer auto-places components with semantic constraints, routes wires, uses real KiCad symbols (light theme), and has a multi-level symbol lookup:
  1. Exact match in the requested library (e.g. Device:R, RF:SX1262IMLTRT)
  2. Fuzzy/prefix search across ALL KiCad library files, as long as the match still contains the requested reference/name
  3. Custom JSON symbol definitions created with component_def
  4. Generic IC box fallback (numbered pins, correct wiring)

When generating a schematic answer, keep it tight: output only fenced `component_def` and `netlist` blocks, plus at most one short intro line if it adds real value. Never emit a bare `component_def` heading, raw YAML outside a fence, or a long explanatory preamble.
Prefer a real KiCad library symbol whenever a close match exists; use `component_def` only as a last resort when no library symbol fits after exact and fuzzy lookup.
For the main component the user asked for, always prefer the most complete official KiCad symbol available. Do not replace a real part with a simplified 4-5 pin generic symbol if the library contains a fuller symbol matching the requested part. Reserve simplified MCU/IC placeholders for supporting blocks, adapters, or cases where no complete library symbol exists.
If the requested part is a named device with a datasheet, map its pins from the datasheet onto the complete symbol pins as faithfully as possible; do not collapse it to a generic box unless there is truly no library symbol to use.

━━━ Netlist syntax ━━━
    Ref  Lib:Symbol  Value  pin1_net  pin2_net  [pin3_net ...]  [key=value ...]
    label  net_name  display_text

Rules:
- Every component line must include an explicit `role=...` metadata field.
- Use `role=none` when no special placement behavior is needed.
- Pin nets are listed in KiCad pin order (pin 1 first).
- Power nets auto-detected: VDD/VCC/+5V/+3V3 → top; GND/AGND/VSS → bottom.
- Use `role=main` for the central IC/module, `role=connector|input|output|source|load` for external interfaces.
- Use `role=bypass` or `role=decouple` for local power capacitors, and always add `near=U1.8:100` when the target power pin is known.
- Use `role=bulk` for reservoir capacitors, `role=pullup|pulldown` for bias resistors, `role=series|filter|termination` for inline signal parts.
- Use `near=REF.pin:strength` to request local placement near a specific target pin; 100 means strongest proximity.
- Use `net NET role=bus label=TEXT` for protocol nets that should be labeled locally rather than routed as one long shared wire.
- Keep one canonical name for each electrical net. If a signal is called `RX0` on one side, do not rename the same wire to `UART_RX` on another side; reuse the same net name and the same label everywhere on that connection.
- For the main module or IC, treat the symbol pin names as authoritative. Never swap `GND` with `3V3`, `VCC`, or `VDD`, and never infer power pin order from the left/right visual layout alone.
- If a symbol already exposes a standard interface name such as `RX0`, `TX0`, `UART_RX`, or `UART_TX`, keep the chosen canonical net name consistent end-to-end instead of inventing a second alias on the connector side.
- Every attachment point must have at least one straight net segment before any bend: IC pins, passives, connectors, power symbols, and grouped bypass rails.
- Never attach a perpendicular net directly to a pin or power symbol; draw straight out first, then bend at 90° maximum.
- For side-facing IC power pins, use a straight stub with a side-oriented power symbol and label (`VSS`, `GND`, `VDD`), never a vertical power symbol that would require an elbow.
- Never reuse an older rendered image as an answer artifact. Regenerate the schematic illustration from the current message every time.
- Never create 180° U-turns in wires.
- Use `NC` only for intentionally unconnected pins; `NC` pins are not electrical nets and should not be labeled or routed.
- Do not create one-endpoint signal nets unless you explicitly want a visible local label.
- Put grouped bypass capacitors on one shared rail when they target the same power pin/net.
- No coordinates — layout is automatic.
- Max ~10 components for a readable diagram.

━━━ Unknown IC workflow ━━━
When the user asks about a component NOT in the Device library (e.g. SX1262, ESP32, STM32…):
1. Search the datasheet online, preferably with UltraLibrarian first, and then open the matching part page.
2. Find the pin table and a "typical application" circuit.
3. Output a ```component_def block FIRST to define the symbol.
4. Then output the ```netlist block with the typical application circuit.

component_def syntax:
```component_def
name: SX1262
description: LoRa sub-GHz transceiver (Semtech)
source_url: https://semtech.com/...
pins:
  1: VDD
  2: GND
  3: NSS
  4: SCK
  5: MOSI
  6: MISO
  7: BUSY
  8: DIO1
  9: NRESET
  10: RF_IO
```

Once defined, future netlist blocks can reference: `Semtech:SX1262` — the cached pins will be used.

━━━ Passive symbol names (Device library) ━━━
    R, C, C_Polarized, L, D, LED, Zener, Q_NPN_BCE, Q_PNP_BCE, SW_Push

━━━ Example — RC low-pass filter ━━━
```netlist
R1  Device:R  10k    VCC   Vout  role=none
C1  Device:C  100nF  Vout  GND   role=none
label Vout Vout
```

━━━ Example — MCU bypass capacitor ━━━
```netlist
U1  MCU:ESP32  ESP32  VDD  GND  IO0  SCL  SDA  role=main
C1  Device:C   100nF  VDD  GND                 role=bypass near=U1.1:100
R1  Device:R   4k7    VDD  SCL                 role=pullup near=U1.4:85
J1  Connector:Conn_01x04  I2C  VDD  GND  SCL  SDA  role=connector
net SCL role=bus label=SCL
net SDA role=bus label=SDA
```

Fallback — ```schematic  (raw SVG, last resort only):
No <script>, no event handlers, background #f5f4ef.
"""


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_context_file(context_dir: str, filename: str) -> str:
    session_path = os.path.join(context_dir, filename)
    if os.path.exists(session_path):
        return session_path

    # Fallback: shared exports in ai_review_context root.
    # context_dir shape: .../ai_review_context/chats/<session_id>
    parent = os.path.dirname(context_dir)
    if os.path.basename(parent) == "chats":
        root_dir = os.path.dirname(parent)
        shared_path = os.path.join(root_dir, filename)
        if os.path.exists(shared_path):
            return shared_path

    return session_path


def _build_context_summary(context_dir: str, on_status=None) -> Dict[str, Any]:
    def _s(msg: str) -> None:
        if on_status:
            on_status(msg)

    _s("Analyzing project context…")
    project_path = _resolve_context_file(context_dir, "project.json")
    pcb_path = _resolve_context_file(context_dir, "pcb.json")
    review_path = _resolve_context_file(context_dir, "review.json")
    drc_run_path = _resolve_context_file(context_dir, "drc_run.json")
    erc_run_path = _resolve_context_file(context_dir, "erc_run.json")

    project = _read_json(project_path)

    _s("Analyzing PCB layout…")
    pcb = _read_json(pcb_path)

    _s("Loading previous findings…")
    review = _read_json(review_path)

    _s("Loading DRC / ERC reports…")
    drc_run = _read_json(drc_run_path)
    erc_run = _read_json(erc_run_path)

    has_drc_run = bool(drc_run)
    has_erc_run = bool(erc_run)
    drc_status = "unknown"
    erc_status = "unknown"
    if has_drc_run:
        drc_status = "ok" if drc_run.get("ok", False) else "failed"
    if has_erc_run:
        erc_status = "ok" if erc_run.get("ok", False) else "failed"

    return {
        "available_context": {
            "project_json": os.path.exists(project_path),
            "pcb_json": os.path.exists(pcb_path),
            "review_json": os.path.exists(review_path),
            "drc_run_json": os.path.exists(drc_run_path),
            "erc_run_json": os.path.exists(erc_run_path),
        },
        "project_path": project.get("project_path", ""),
        "board_file": project.get("board_file", ""),
        "kicad_version": project.get("kicad_version", ""),
        # Compact component list — ref + value only (sufficient for most questions).
        # Use {read_pcb} to get full data: footprint, layer, x/y coordinates.
        "components": [
            {"ref": fp.get("ref", ""), "value": fp.get("value", "")}
            for fp in pcb.get("footprints", [])[:300]
        ],
        "net_names": [
            n.get("name", "") for n in pcb.get("nets", [])
            if n.get("name", "").strip()
        ][:150],
        "footprints_total": len(pcb.get("footprints", [])),
        "tracks": len(pcb.get("tracks", [])),
        "vias": len(pcb.get("vias", [])),
        "zones": len(pcb.get("zones", [])),
        "nets": len(pcb.get("nets", [])),
        "latest_findings": review.get("findings", [])[:20],
        "drc_status": drc_status,
        "drc_issue_count": drc_run.get("issue_count", 0),
        "erc_status": erc_status,
        "erc_issue_count": erc_run.get("issue_count", 0),
    }


def _sanitize_history_for_api(
    history: List[Dict[str, Any]],
    max_messages: int = 14,
) -> List[Dict[str, Any]]:
    """Return a tail of messages that is safe to send to OpenAI.

    The chat history may contain internal tool-loop messages. If we truncate
    the history in the middle of a tool sequence, OpenAI rejects the payload
    when it sees a `tool` message without the preceding assistant message that
    declared `tool_calls`.

    This helper keeps the most recent messages while dropping orphan `tool`
    messages that no longer have a matching assistant/tool-call context in the
    selected tail.
    """
    _API_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}

    sanitized: List[Dict[str, Any]] = []
    for raw_msg in history[-max_messages:]:
        msg = {k: v for k, v in raw_msg.items() if k in _API_KEYS}
        if not isinstance(msg.get("content"), (str, list)):
            msg["content"] = str(msg.get("content") or "")
        sanitized.append(msg)

    filtered: List[Dict[str, Any]] = []
    pending_tool_ids: Optional[set] = None

    for msg in sanitized:
        role = str(msg.get("role", ""))

        if role == "assistant" and msg.get("tool_calls"):
            filtered.append(msg)
            pending_tool_ids = {
                str(tc.get("id", ""))
                for tc in msg.get("tool_calls", [])
                if isinstance(tc, dict) and tc.get("id")
            } or None
            continue

        if role == "tool":
            tool_call_id = str(msg.get("tool_call_id", ""))
            if pending_tool_ids and tool_call_id in pending_tool_ids:
                filtered.append(msg)
                continue
            # Orphan tool message: drop it so the request stays valid.
            continue

        pending_tool_ids = None
        filtered.append(msg)

    return filtered


# ---------------------------------------------------------------------------
# TOOLS — OpenAI function-calling schemas
# ---------------------------------------------------------------------------

def _no_params_tool(name: str, description: str) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _tool(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOLS: List[Dict[str, Any]] = [
    # ── Read / export tools ──────────────────────────────────────────────────
    _no_params_tool("read_pcb",
        "Read the PCB: full component list (ref, value, footprint, position, layer), "
        "net names, track count, via count. Use when you need PCB data to answer a question."),
    _no_params_tool("read_project",
        "Read project metadata: schematic files, project variables, board filename, KiCad version."),
    _no_params_tool("execute_drc",
        "Run a Design Rules Check and return all violations with rule name and description."),
    _no_params_tool("execute_erc",
        "Run an Electrical Rules Check and return all violations."),
    _no_params_tool("execute_full",
        "Full project read + PCB data + automated DRC/ERC review in one step."),
    _no_params_tool("export_pdf",
        "Export the PCB layout as PDF, one page per active layer."),
    _no_params_tool("export_images",
        "Export each PCB layer as a PNG image so you can visually inspect copper routing, "
        "silkscreen, solder mask, courtyards, etc. Call this ANY time the user asks to see "
        "the PCB, check the routing, look at layers, or requests a visual inspection."),
    _no_params_tool("read_design_rules",
        "Read all net classes (name, clearance, track width, via sizes), board minimums, "
        "and current DRC severity levels. Always call this before modifying design rules."),
    _no_params_tool("read_net_class_assignments",
        "List which nets are assigned to which net class."),
    _tool(
        "ultralibrarian_search",
        "Search UltraLibrarian's public CAD library for a part number or manufacturer and return matching detail URLs, descriptions, and availability flags.",
        {
            "query": {
                "type": "string",
                "description": "Part number, manufacturer, or search phrase.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of search results to return.",
            },
        },
        ["query"],
    ),
    _tool(
        "ultralibrarian_get_part",
        "Open a specific UltraLibrarian part detail page and return manufacturer, part name, datasheet page URL, preview information, and CAD availability.",
        {
            "detail_url": {
                "type": "string",
                "description": "UltraLibrarian part detail URL.",
            },
        },
        ["detail_url"],
    ),
    _tool(
        "ultralibrarian_lookup",
        "One-shot UltraLibrarian search + detail lookup for a part number or manufacturer.",
        {
            "query": {
                "type": "string",
                "description": "Part number, manufacturer, or search phrase.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of search results to consider.",
            },
        },
        ["query"],
    ),
    # ── Write tools ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {            "name": "execute_pcbnew_script",
            "description": (
                "Execute arbitrary Python code in KiCad's live scripting context. "
                "`board` (pcbnew.BOARD) and `pcbnew` (the full module) are pre-injected. "
                "Use for any PCB modification: add or edit text/graphics/tracks/vias/footprints/"
                "zones/pads/net classes/DRC rules/board outline/copper fills or anything else "
                "the pcbnew Python API supports. Print values to capture them in the output. "
                "Call save_board after changes to persist them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute. `board` and `pcbnew` are already available.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {            "name": "modify_netclass",
            "description": (
                "Modify the clearance, track width, and/or via sizes of a net class. "
                "ALWAYS call read_design_rules first to confirm the current values. "
                "ALWAYS call save_board after a successful modification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "netclass_name": {
                        "type": "string",
                        "description": "Net class name, e.g. 'Default', 'Power', 'RF'. Case-sensitive.",
                    },
                    "clearance_mm": {
                        "type": "number",
                        "description": "New copper clearance in millimetres.",
                    },
                    "track_width_mm": {
                        "type": "number",
                        "description": "New track width in millimetres.",
                    },
                    "via_diameter_mm": {
                        "type": "number",
                        "description": "New via diameter in millimetres.",
                    },
                    "via_drill_mm": {
                        "type": "number",
                        "description": "New via drill hole diameter in millimetres.",
                    },
                },
                "required": ["netclass_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_drc_severity",
            "description": (
                "Change the severity level of a DRC rule type. "
                "Valid severities: 'error', 'warning', 'ignore'. "
                "ALWAYS call save_board after a successful modification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "violation_type": {
                        "type": "string",
                        "description": (
                            "Rule type to change. One of: clearance, track_width, min_hole, "
                            "copper_edge_clearance, silk_over_copper, net_conflict, "
                            "diff_pair_gap, diff_pair_uncoupled, courtyards_overlap, "
                            "starved_thermal, unconnected_items, footprint."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["error", "warning", "ignore"],
                        "description": "New severity level.",
                    },
                },
                "required": ["violation_type", "severity"],
            },
        },
    },
    _no_params_tool("save_board",
        "Save all pending modifications to the .kicad_pcb file."),
]


def ask_ai(
    context_dir: str,
    history: List[Dict[str, Any]],
    board_file: str = "",
    on_status=None,
    on_chunk=None,
    on_thinking=None,
) -> Dict[str, Any]:
    settings = PluginSettings()
    cfg = settings.load_runtime_config()

    summary = _build_context_summary(context_dir, on_status=on_status)
    context_text = json.dumps(summary, ensure_ascii=False, indent=2)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_GOAL},
        {
            "role": "system",
            "content": "Project context summary (JSON):\n" + context_text,
        },
    ]

    # Keep a short rolling memory while preserving valid tool sequences.
    messages.extend(_sanitize_history_for_api(history, max_messages=14))

    if on_status:
        on_status("Thinking…")

    client = LLMClient(
        provider=str(cfg.get("provider", "openai")),
        api_key=str(cfg.get("openai_api_key", "")),
        model=str(cfg.get("openai_model", "gpt-4.1-mini")),
        temperature=float(cfg.get("temperature", 0.2)),
    )

    if on_chunk is not None or on_thinking is not None:
        return client.stream_chat(messages, on_chunk=on_chunk, on_thinking=on_thinking,
                                  tools=TOOLS)
    return client.chat(messages, tools=TOOLS)


def suggest_chat_title(context_dir: str, history: List[Dict[str, str]]) -> str:
    settings = PluginSettings()
    cfg = settings.load_runtime_config()

    summary = _build_context_summary(context_dir)
    context_text = json.dumps(summary, ensure_ascii=False, indent=2)

    messages: List[Dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Generate a short chat title for an electronics engineering conversation. "
                "Return only the title, no quotes, no markdown, max 50 characters."
            ),
        },
        {
            "role": "system",
            "content": "Project context summary (JSON):\n" + context_text,
        },
    ]

    # Keep a concise but meaningful slice.
    messages.extend(history[-8:])

    client = LLMClient(
        provider=str(cfg.get("provider", "openai")),
        api_key=str(cfg.get("openai_api_key", "")),
        model=str(cfg.get("openai_model", "gpt-4.1-mini")),
        temperature=float(cfg.get("temperature", 0.2)),
    )

    result = client.chat(messages)
    if not result.get("ok", False):
        return ""

    raw = str(result.get("content", "")).strip()
    if not raw:
        return ""

    # Normalize one-line plain title.
    title = " ".join(raw.splitlines()).strip().strip('"').strip("'")
    return title[:50]
