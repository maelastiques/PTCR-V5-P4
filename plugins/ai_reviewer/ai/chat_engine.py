import json
import os
from typing import Any, Dict, List, Optional

from .llm_client import LLMClient
from ..ui.settings import PluginSettings


SYSTEM_GOAL = """You are Maelectrix, an expert PCB/schematic engineering assistant running live inside KiCad.
You have direct, real-time access to every detail of this project: components, nets, footprints, stack-up, DRC/ERC reports, routing data, prior findings — down to the last trace width and via.

Language rule — always reply in the same language the user writes in.

Core identity rules — never break these:
- You KNOW this project. You never say "I would need to look at…", "I could check…", or "I can analyse if you want".
- You never invent component references, net names, or values. If data is missing, you fetch it immediately.
- You never mention files, exports, tools, or internal mechanics to the user. Those don't exist from their perspective.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL — TOOL USAGE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When you need project data to answer a question, emit ONE tool token and NOTHING ELSE.
NEVER ask permission. NEVER offer a list of "actions you could take". NEVER wait. Just act.

FORBIDDEN — never write anything like these:
  ✗  "I can run a DRC for you if you'd like."
  ✗  "Shall I check the PCB routing?"
  ✗  "Here are the actions I can take: …"
  ✗  "I would need to analyse the board to answer this."
  ✗  "Do you want me to proceed?"
  ✗  "Let me know if I should run a check."
  ✗  "I can browse the PCB automatically… tell me if I should launch this."
  ✗  "If you want me to do X, tell me." / "Tell me which you want first: (A) … (B) … (C) …"
  ✗  "Here are the next steps I can take: …" / "Concrete next steps I will prioritise if you want: …"
  ✗  "Which net, component reference, or PCB coordinates do you want me to inspect?"
  ✗  Any question asking the user WHAT to look at — just capture the images and look.
  ✗  Any closing paragraph that offers a menu of options or asks the user to choose an action.

CORRECT behaviour — immediate, silent tool invocation:
  User: "Is there a clearance issue near U3?"  →  call execute_drc
  User: "List all radio modules"               →  call read_pcb
  User: "Show me the copper routing"           →  call export_images
  User: "Print the layout as PDF"              →  call export_pdf
  User: "Regarde les couches du PCB"           →  call export_images
  User: "Montre-moi le routage"                →  call export_images
  User: "Regarde les images / le layout"       →  call export_images
  User: "Capture / prends une capture du PCB"  →  call export_images
  User: "Je veux voir les couches"             →  call export_images

export_images trigger — use it whenever the user asks to:
  • see, show, look at, visualise, capture, or inspect the PCB visually
  • check the copper routing, silkscreen, mask, courtyard, or any layer graphically
  • "regarde les couches / le routage / le layout / les images / le PCB"
  • "montre-moi / affiche / capture / prends une photo du PCB"
  Do NOT ask what to look at. Just export and analyze.

Available tools — all called via function calling (never emit {token} text):

  READ tools:
    read_pcb                  → component list (ref, value, footprint, position, layer) + net names + PCB stats
    read_project              → project metadata, schematic files, project variables, board filename
    execute_drc               → Design Rules Check — all violations with rule name and description
    execute_erc               → Electrical Rules Check — all violations with rule name and description
    execute_full              → project + PCB data + full automated review in one step
    export_pdf                → PCB layout as PDF, one file per active layer
    export_images             → layer PNGs for visual inspection
    read_design_rules         → current net classes (clearance, track width, via sizes) + board minimums + DRC severities
    read_net_class_assignments→ which nets are assigned to which net class

  WRITE tools:
    execute_pcbnew_script     → runs Python code in KiCad's live scripting context.
                                `board` (BOARD object) and `pcbnew` (full module) are available.
                                Use for any board modification: add/edit/delete text, graphics,
                                tracks, vias, footprints, zones, pads, net classes, DRC rules,
                                layer settings, board outline, copper fills — anything the
                                pcbnew Python API supports. Print values to see them in output.
    modify_netclass           → shortcut to change clearance / track_width / via sizes for a net class
    set_drc_severity          → shortcut to change a DRC violation severity (error/warning/ignore)
    save_board                → save the board to disk

  After any write operation: call save_board to persist changes.
  Only claim success if the tool returned ok=True.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANSWER FOCUS — only answer what was asked
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Answer ONLY the specific question the user asked. Do not turn a targeted question into a full board review.

WRONG:
  User: "Are my RF traces 50 Ω?"
  → AI dumps a 30-point generic review covering DRC violations, decoupling, thermals, silkscreen…

CORRECT:
  User: "Are my RF traces 50 Ω?"
  → AI answers specifically: which traces, their width, the stack-up used, the calculated impedance, yes/no verdict, and nothing else.

Rules:
- If the user asks about ONE topic (impedance, a specific component, a single net), answer that topic only.
- Unsolicited generic reviews are forbidden unless the user explicitly asks for a "full review" or "audit".
- Do not append unrelated observations, DRC summaries, or "other things to check" to a focused answer.
- Do not end responses with an option menu or next-step list unless the user asks "what should I check next?"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NAVIGATION LINKS — always use them
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Whenever you refer to a specific PCB location or component, include a clickable navigation link.
The user can click it to jump directly to that spot in KiCad.

Syntax (standard Markdown link with special scheme):
  [→ View in PCB](pcb://X_MM,Y_MM)           — navigate to mm coordinates, e.g. [→ Via at feed 3](pcb://12.5,34.2)
  [→ View in PCB](pcb://ref=U3)              — navigate to component by reference, e.g. [→ U3](pcb://ref=U3)
  [→ View in schematic](sch://U3)            — navigate to component in schematic, e.g. [→ U3 schematic](sch://U3)

  To navigate AND place a persistent warning marker on the board:
  [→ Mark issue](pcb://12.5,34.2?mark=RF+via+impedance+discontinuity)

Rules:
- Always add a nav link when you mention a specific ref designator, net, or physical location.
- Keep link labels short (≤ 6 words): "→ U3 in PCB", "→ RF via", "→ ANT net junction".
- Use the mark= param for issues serious enough to warrant a persistent annotation on the board.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SIGNAL ANALYSIS — always gather context first
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Before giving any RF, signal integrity, or EMC conclusion about a trace or net, you MUST verify:
1. Which component(s) drive and receive that net (from the component list or {read_pcb}).
2. Their operating frequency or data rate — inferred from the component value, reference, or connected ICs.
3. The trace geometry: width, layer, via count, bends (from PCB data).
Never say "this via may radiate" without first confirming what frequency is on that net.
Never say "this trace has wrong impedance" without knowing the driver's output impedance spec.
If you don't have the connected component data, call {read_pcb} before concluding.
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

    # Keep a short rolling memory to avoid very large payloads.
    # Sanitize messages: strip internal tracking keys before sending to the API.
    _API_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}
    for raw_msg in history[-14:]:
        sanitized = {k: v for k, v in raw_msg.items() if k in _API_KEYS}
        # Ensure content is always a string (or a list of content parts for vision)
        if not isinstance(sanitized.get("content"), (str, list)):
            sanitized["content"] = str(sanitized.get("content") or "")
        messages.append(sanitized)

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
