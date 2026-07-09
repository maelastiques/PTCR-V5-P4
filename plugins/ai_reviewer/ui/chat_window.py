import base64
import copy
import json
import os
import re
import shutil
import threading
import uuid
import webbrowser
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import wx
import wx.html2
import wx.stc

from ..ai.action_executor import execute_action
from ..ai.chat_engine import ask_ai, suggest_chat_title
from .image_tools import add_svg_marker, export_board_svg
from .markdown_render import build_theme, markdown_to_html, render_conversation
from .settings import PluginSettings
from .settings_window import open_settings_dialog
from .widgets import FlatButton, RoundedPanel, hex_to_colour, is_dark_colour


COMMAND_PATTERN = re.compile(
    r"\{\s*(execute_full|execute_drc|execute_erc|read_project|read_pcb|run_drc|run_erc|export_pdf|export_images?|ultralibrarian_search|ultralibrarian_get_part|ultralibrarian_lookup)\s*\}",
    re.IGNORECASE,
)

# Approximate OpenAI pricing in USD per 1M tokens.
MODEL_PRICING_PER_1M = {
    "gpt-5": {"input": 5.00, "output": 15.00},
    "gpt-5-mini": {"input": 0.30, "output": 1.20},
    "gpt-5-nano": {"input": 0.05, "output": 0.20},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4o": {"input": 5.00, "output": 15.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "o3": {"input": 2.00, "output": 8.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 0.60, "output": 2.40},
}

WELCOME_HINT = (
    "Ask a question about your schematic or PCB. "
    "Nothing runs automatically — {execute_full}, {execute_drc} and {execute_erc} "
    "are only triggered when the analysis requires it."
)


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _model_pricing(model: str) -> Dict[str, float]:
    model_key = (model or "").strip().lower()
    if model_key in MODEL_PRICING_PER_1M:
        return MODEL_PRICING_PER_1M[model_key]

    for known, pricing in MODEL_PRICING_PER_1M.items():
        if model_key.startswith(known):
            return pricing

    return {"input": 0.0, "output": 0.0}


class ChatFrame(wx.Frame):
    def __init__(self, board_file: str, context_dir: str) -> None:
        super().__init__(None, title="Maelectrix", size=(1140, 760))
        self.board_file = board_file
        self.context_root_dir = context_dir
        self.chats_root_dir = os.path.join(self.context_root_dir, "chats")
        self.current_session_id = ""
        self.current_session_dir = ""
        self.current_session_meta: Dict[str, Any] = {}
        self.history: List[Dict[str, str]] = []
        self._session_items: List[Dict[str, Any]] = []
        self._is_busy = False
        self._stream_content = ""
        self._stream_pending = False
        self._scroll_pending = True
        self._render_token = 0
        self._spend_total_usd = 0.0
        self._spend_total_dirty = True
        self.is_macos = wx.Platform == "__WXMAC__"

        self.theme_bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        self.theme_is_dark = is_dark_colour(self.theme_bg)
        self.theme = build_theme(self.theme_is_dark)
        self._load_theme_assets()
        self._settings = PluginSettings()

        # Derived wx colours for native widgets.
        self.c_app = hex_to_colour(self.theme["app_bg"])
        self.c_surface = hex_to_colour(self.theme["surface"])
        self.c_surface2 = hex_to_colour(self.theme["surface_2"])
        self.c_text = hex_to_colour(self.theme["text"])
        self.c_muted = hex_to_colour(self.theme["muted"])
        self.c_accent = hex_to_colour(self.theme["accent"])
        self.c_border = hex_to_colour(self.theme["border"])

        self.SetBackgroundColour(self.c_app)
        self.SetMinSize((880, 560))

        self._build_ui()

        self._init_chat_sessions()
        wx.CallAfter(self.input.SetFocus)
        # macOS: force TextCtrl colours once the event loop is running (NSTextView
        # ignores SetBackgroundColour when called during __init__).
        wx.CallAfter(self._apply_input_colours)

    # ------------------------------------------------------------------
    # Theme asset loading (background image + AI avatar)
    # ------------------------------------------------------------------
    def _load_theme_assets(self) -> None:
        """Read background PNG and avatar SVG from images/ and embed in theme dict."""
        images_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "images")

        # Background image — use file:// URL (lightweight; base URL passed to SetPage)
        bg_name = "background-dark.png" if self.theme_is_dark else "background-light.png"
        bg_path = os.path.join(images_dir, bg_name)
        if os.path.exists(bg_path):
            self.theme["bg_image"] = 'url("{}")'.format("file://" + bg_path)

        # AI avatar — base64 SVG (49 KB, fine for inline use)
        avatar_path = os.path.join(images_dir, "icon-m8.svg")
        if os.path.exists(avatar_path):
            try:
                with open(avatar_path, "rb") as fh:
                    svg_b64 = base64.b64encode(fh.read()).decode("ascii")
                self.theme["avatar_src"] = "data:image/svg+xml;base64,{}".format(svg_b64)
            except Exception:
                pass  # fallback: "AI" text (default "" from build_theme)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _apply_input_colours(self) -> None:
        """Style the StyledTextCtrl input once the event loop is running.

        wx.stc.StyledTextCtrl uses Scintilla for rendering (completely
        independent of macOS NSTextView), so colours apply reliably.
        """
        try:
            stc = self.input
            bg  = self.c_surface
            fg  = self.c_text

            # Hide all margins (line numbers, symbols, fold markers)
            for i in range(5):
                stc.SetMarginWidth(i, 0)

            # Font — larger than system default for comfortable reading
            stc.StyleSetSize(wx.stc.STC_STYLE_DEFAULT, 14)
            stc.StyleSetFaceName(wx.stc.STC_STYLE_DEFAULT,
                wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT).GetFaceName())

            # Apply default style to every Scintilla style slot
            stc.StyleSetBackground(wx.stc.STC_STYLE_DEFAULT, bg)
            stc.StyleSetForeground(wx.stc.STC_STYLE_DEFAULT, fg)
            stc.StyleClearAll()
            stc.SetBackgroundColour(bg)
            stc.SetForegroundColour(fg)

            # Vertical centering: add extra space above text so a single line
            # of 14pt sits in the middle of the ~52px control area.
            try:
                stc.SetExtraAscent(10)
                stc.SetExtraDescent(2)
            except Exception:
                pass

            # Caret
            stc.SetCaretForeground(self.c_accent)
            stc.SetCaretWidth(2)

            # Selection
            sel_bg = wx.Colour(
                min(255, self.c_accent.Red() + 20),
                min(255, self.c_accent.Green() + 20),
                min(255, self.c_accent.Blue() + 20),
                80,
            )
            stc.SetSelBackground(True, sel_bg)
            stc.SetSelForeground(False, fg)

            # Layout / scrollbars
            stc.SetScrollWidthTracking(True)
            stc.SetUseHorizontalScrollBar(False)
            stc.SetUseVerticalScrollBar(True)
            stc.SetWrapMode(wx.stc.STC_WRAP_WORD)
            stc.SetWrapVisualFlags(wx.stc.STC_WRAPVISUALFLAG_NONE)
            stc.SetEndAtLastLine(True)

            # No visual clutter
            stc.SetViewWhiteSpace(wx.stc.STC_WS_INVISIBLE)
            stc.SetViewEOL(False)
            stc.SetIndentationGuides(0)

            stc.Refresh()
        except Exception:
            pass
        # Show placeholder once colours are applied
        wx.CallAfter(self._show_placeholder)

    # ------------------------------------------------------------------
    # Placeholder helpers
    # ------------------------------------------------------------------
    _PLACEHOLDER_TEXT = "Ask a question…"

    def _show_placeholder(self) -> None:
        """Display muted placeholder text in the input (read-only, non-editable)."""
        try:
            stc = self.input
            # Never cover the input with placeholder while it has keyboard focus
            if self.FindFocus() is stc:
                return
            stc.SetReadOnly(False)  # must be writable to call SetText
            stc.StyleSetForeground(wx.stc.STC_STYLE_DEFAULT, self.c_muted)
            stc.StyleClearAll()
            stc.SetText(self._PLACEHOLDER_TEXT)
            stc.GotoPos(0)
            stc.SetReadOnly(True)   # lock — user cannot type over / delete it
            self._placeholder_active = True
        except Exception:
            pass

    def _hide_placeholder(self) -> None:
        """Clear placeholder and restore normal text colour and editability."""
        if not self._placeholder_active:
            return
        try:
            stc = self.input
            stc.SetReadOnly(False)  # restore before clearing
            stc.StyleSetForeground(wx.stc.STC_STYLE_DEFAULT, self.c_text)
            stc.StyleClearAll()
            stc.SetText("")
            self._placeholder_active = False
        except Exception:
            pass

    def _on_input_focus(self, event) -> None:
        self._hide_placeholder()
        event.Skip()

    def _on_input_blur(self, event) -> None:
        if not self.input.GetText().strip():
            self._show_placeholder()
        event.Skip()

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        panel.SetBackgroundColour(self.c_app)
        root = wx.BoxSizer(wx.HORIZONTAL)

        root.Add(self._build_sidebar(panel), 0, wx.EXPAND | wx.ALL, 10)
        root.Add(self._build_main(panel), 1, wx.EXPAND | wx.TOP | wx.BOTTOM | wx.RIGHT, 10)

        panel.SetSizer(root)

    def _build_sidebar(self, parent: wx.Window) -> wx.Window:
        side = wx.Panel(parent)
        side.SetBackgroundColour(self.c_surface)
        side.SetMinSize((236, -1))
        sizer = wx.BoxSizer(wx.VERTICAL)

        brand_row = wx.BoxSizer(wx.HORIZONTAL)
        # SVG icon before the brand name
        _icon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "images", "icon-m8.svg")
        if os.path.exists(_icon_path):
            try:
                _bmp_bundle = wx.BitmapBundle.FromSVGFile(_icon_path, wx.Size(28, 28))
                if _bmp_bundle.IsOk():
                    brand_row.Add(wx.StaticBitmap(side, bitmap=_bmp_bundle), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            except Exception:
                pass

        brand = wx.StaticText(side, label="Maelectrix")
        brand.SetForegroundColour(self.c_text)
        brand_font = brand.GetFont()
        brand_font.SetPointSize(brand_font.GetPointSize() + 3)
        brand_font.MakeBold()
        brand.SetFont(brand_font)
        brand_row.Add(brand, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(brand_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)

        subtitle = wx.StaticText(side, label="KiCad AI")
        subtitle.SetForegroundColour(self.c_muted)
        sizer.Add(subtitle, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        self.btn_new_chat = FlatButton(
            side, "＋   New chat", self.c_accent, wx.Colour(255, 255, 255),
            self._on_new_chat, radius=11, min_size=(-1, 42),
        )
        sizer.Add(self.btn_new_chat, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        section = wx.StaticText(side, label="CONVERSATIONS")
        section.SetForegroundColour(self.c_muted)
        section_font = section.GetFont()
        section_font.SetPointSize(max(8, section_font.GetPointSize() - 2))
        section.SetFont(section_font)
        sizer.Add(section, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 16)

        self.chat_list = wx.ListBox(side, style=wx.BORDER_NONE)
        self.chat_list.SetBackgroundColour(self.c_surface)
        self.chat_list.SetForegroundColour(self.c_text)
        self.chat_list.Bind(wx.EVT_LISTBOX, self._on_select_chat)
        sizer.Add(self.chat_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.btn_delete_chat = FlatButton(
            side, "Delete conversation", self.c_surface2, self.c_muted,
            self._on_delete_chat, radius=9, bold=False, min_size=(-1, 34),
            flat=True,
        )
        sizer.Add(self.btn_delete_chat, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

        divider = wx.Panel(side, size=(-1, 1))
        divider.SetBackgroundColour(self.c_border)
        sizer.Add(divider, 0, wx.EXPAND | wx.ALL, 12)

        self.btn_settings = FlatButton(
            side, "⚙   Settings", self.c_surface2, self.c_text,
            self._on_settings, radius=9, bold=False, min_size=(-1, 36), flat=True,
        )
        sizer.Add(self.btn_settings, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        side.SetSizer(sizer)
        return side

    def _build_main(self, parent: wx.Window) -> wx.Window:
        main = wx.Panel(parent)
        main.SetBackgroundColour(self.c_app)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Header row.
        header = wx.BoxSizer(wx.HORIZONTAL)
        self.title_label = wx.StaticText(main, label="New chat")
        self.title_label.SetForegroundColour(self.c_text)
        title_font = self.title_label.GetFont()
        title_font.SetPointSize(title_font.GetPointSize() + 2)
        title_font.MakeBold()
        self.title_label.SetFont(title_font)
        header.Add(self.title_label, 1, wx.ALIGN_CENTER_VERTICAL)

        self.budget_info = wx.StaticText(main, label="Budget : n/a")
        self.budget_info.SetForegroundColour(self.c_muted)
        header.Add(self.budget_info, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        sizer.Add(header, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)

        # Conversation WebView.
        self.webview = wx.html2.WebView.New(main)
        self.webview.SetBackgroundColour(self.c_app)
        try:
            self.webview.EnableContextMenu(False)
        except Exception:
            pass
        self.webview.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_webview_nav)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_LOADED, self._on_webview_loaded)
        sizer.Add(self.webview, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        # Input bar.
        input_card = RoundedPanel(main, bg=self.c_surface, border=self.c_border, radius=16)
        input_row = wx.BoxSizer(wx.HORIZONTAL)

        self._placeholder_active = False
        self.input = wx.stc.StyledTextCtrl(input_card, style=wx.BORDER_NONE)
        self.input.SetMinSize((-1, 52))
        self.input.Bind(wx.EVT_KEY_DOWN, self._on_input_key)
        self.input.Bind(wx.EVT_SET_FOCUS, self._on_input_focus)
        self.input.Bind(wx.EVT_KILL_FOCUS, self._on_input_blur)
        input_row.Add(self.input, 1, wx.ALL | wx.EXPAND, 10)

        self.btn_marker = FlatButton(
            input_card, "Capture", self.c_surface2, self.c_muted, self._on_marker,
            radius=10, bold=False, min_size=(92, 40), flat=True, border=self.c_border,
        )
        input_row.Add(self.btn_marker, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        self.btn_send = FlatButton(
            input_card, "Send", self.c_accent, wx.Colour(255, 255, 255), self._on_send,
            radius=10, min_size=(104, 40),
        )
        input_row.Add(self.btn_send, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        input_card.SetSizer(input_row)
        sizer.Add(input_card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)

        main.SetSizer(sizer)
        return main

    # ------------------------------------------------------------------
    # WebView conversation rendering
    # ------------------------------------------------------------------
    def _view_messages_from(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build the list of renderable messages (index-aligned with history).

        Internal tool-loop messages (_hidden=True) are excluded from the view
        so the user only sees user messages and the final AI responses.
        """
        view: List[Dict[str, Any]] = []
        for index, item in enumerate(history):
            if item.get("_hidden"):
                continue
            role = str(item.get("role", "assistant"))
            if role == "error":
                content = self._format_error_card(str(item.get("content", "")), str(item.get("details", "")))
                view.append({"role": "error", "content": content, "_history_index": index})
            else:
                entry: Dict[str, Any] = {"role": role, "content": str(item.get("content", ""))}
                entry["_history_index"] = index
                try:
                    cost = float(item.get("prompt_cost_usd", 0.0) or 0.0)
                    if cost > 0.0:
                        entry["prompt_cost_usd"] = cost
                except Exception:
                    pass
                view.append(entry)
        return view

    def _view_messages(self) -> List[Dict[str, Any]]:
        return self._view_messages_from(self.history)

    def _needs_render_artifact(self, item: Dict[str, Any]) -> bool:
        if str(item.get("role", "")).lower() != "assistant":
            return False
        raw = str(item.get("content", ""))
        if not raw:
            return False
        return any(token in raw for token in ("```netlist", "```circuit", "```schematic", "component_def"))

    def _materialize_display_artifacts(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        prepared = copy.deepcopy(history)
        for item in prepared:
            if not isinstance(item, dict) or item.get("_hidden"):
                continue
            role = str(item.get("role", "assistant"))
            if role != "assistant":
                continue
            raw = str(item.get("content", ""))
            if self._needs_render_artifact(item) and not item.get("rendered_content_html"):
                item["rendered_content_html"] = markdown_to_html(raw)
            if item.get("layer_images") and not item.get("layer_images_data"):
                layer_data: Dict[str, str] = {}
                for lname, lpath in list(item.get("layer_images", {}).items())[:6]:
                    src = ""
                    if lpath and os.path.exists(lpath):
                        try:
                            with open(lpath, "rb") as _fh:
                                _b64 = base64.b64encode(_fh.read()).decode("ascii")
                            mime = "image/svg+xml" if lpath.endswith(".svg") else "image/png"
                            src = "data:{};base64,{}".format(mime, _b64)
                        except Exception:
                            src = ""
                    if src:
                        layer_data[str(lname)] = src
                if layer_data:
                    item["layer_images_data"] = layer_data
        return prepared

    def _render_history_worker(self, session_id: str, token: int, history_snapshot: List[Dict[str, Any]], busy: bool) -> None:
        try:
            prepared = self._materialize_display_artifacts(history_snapshot)
            html_doc = render_conversation(
                self._view_messages_from(prepared),
                self.theme,
                typing=busy,
                empty_hint=WELCOME_HINT,
            )
        except Exception:
            return

        wx.CallAfter(self._apply_rendered_history, session_id, token, prepared, html_doc)

    def _apply_rendered_history(
        self,
        session_id: str,
        token: int,
        prepared_history: List[Dict[str, Any]],
        html_doc: str,
    ) -> None:
        if token != self._render_token or session_id != self.current_session_id:
            return

        self.history = prepared_history
        self._scroll_pending = True
        images_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "images")
        self.webview.SetPage(html_doc, "file://" + images_dir + "/")
        try:
            self._save_current_session()
        except Exception:
            pass

    def _render_history(self) -> None:
        self._update_header_stats()
        self._render_token += 1
        token = self._render_token
        session_id = self.current_session_id
        history_snapshot = copy.deepcopy(self.history)
        self.webview.SetPage(
            '<div style="padding:18px;color:#6b7280;font-family:sans-serif;font-size:13px;">Loading conversation…</div>',
            "file://" + os.path.join(os.path.dirname(os.path.dirname(__file__)), "images") + "/",
        )
        threading.Thread(
            target=self._render_history_worker,
            args=(session_id, token, history_snapshot, self._is_busy),
            daemon=True,
        ).start()

    def _scroll_to_bottom(self) -> None:
        try:
            self.webview.RunScript("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass

    def _on_webview_loaded(self, _event: wx.html2.WebViewEvent) -> None:
        if self._scroll_pending:
            self._scroll_pending = False
            self._scroll_to_bottom()
            wx.CallLater(60, self._scroll_to_bottom)

    def _on_webview_nav(self, event: wx.html2.WebViewEvent) -> None:
        url = event.GetURL() or ""
        if url.startswith("app://"):
            event.Veto()
            self._handle_app_url(url)
            return
        if url.startswith("http://") or url.startswith("https://") or url.startswith("mailto:"):
            event.Veto()
            try:
                webbrowser.open(url)
            except Exception:
                pass
            return
        # Allow internal document loads (about:blank / SetPage).
        event.Skip()

    def _handle_app_url(self, url: str) -> None:
        parsed = urlparse(url)
        action = parsed.netloc or parsed.path.lstrip("/")
        params = parse_qs(parsed.query)
        try:
            index = int(params.get("i", ["-1"])[0])
        except Exception:
            index = -1

        if action in ("copy", "edit"):
            if index < 0 or index >= len(self.history):
                return
            if action == "copy":
                self._copy_to_clipboard(str(self.history[index].get("content", "")))
            elif action == "edit":
                self._edit_message(index)

        elif action == "pcb-goto":
            ref = params.get("ref", [""])[0].strip()
            mark_msg = params.get("mark", [""])[0].strip()
            if ref:
                self._goto_pcb_ref(ref, mark_msg or None)
            else:
                try:
                    x = float(params.get("x", [0.0])[0])
                    y = float(params.get("y", [0.0])[0])
                    self._goto_pcb_location(x, y, mark_msg or None)
                except Exception:
                    pass

        elif action == "sch-goto":
            ref = params.get("ref", [""])[0].strip()
            if ref:
                self._goto_sch_ref(ref)

    # ------------------------------------------------------------------
    # KiCad navigation helpers
    # ------------------------------------------------------------------
    def _set_active_layer(self, layer) -> None:
        """Set the active layer in the PCB editor, with fallback via wx top-level windows."""
        try:
            import pcbnew
            board = pcbnew.GetBoard()
            if board is None:
                return
            frame = board.GetParent()
            if frame is not None:
                frame.SetActiveLayer(layer)
                pcbnew.Refresh()
                return
            # Fallback: scan wx top-level windows for the PCB editor frame
            for win in wx.GetTopLevelWindows():
                if hasattr(win, "SetActiveLayer"):
                    try:
                        win.SetActiveLayer(layer)
                        pcbnew.Refresh()
                        return
                    except Exception:
                        pass
        except Exception:
            pass

    def _goto_pcb_location(self, x_mm: float, y_mm: float, mark_msg: "Optional[str]" = None) -> None:
        """Center the PCB editor view on (x_mm, y_mm). Optionally place a marker."""
        try:
            import pcbnew
            board = pcbnew.GetBoard()
            if board is None:
                return
            pos = pcbnew.VECTOR2I(int(pcbnew.FromMM(x_mm)), int(pcbnew.FromMM(y_mm)))
            if mark_msg:
                text = self._place_pcb_marker(pos, mark_msg, board)
                if text is not None:
                    try:
                        pcbnew.FocusOnItem(text, pcbnew.Cmts_User)
                    except Exception:
                        try:
                            pcbnew.FocusOnItem(text)
                        except Exception:
                            pass
                # Defer layer switch so FocusOnItem's internal refresh doesn't override it
                wx.CallLater(80, self._set_active_layer, pcbnew.Cmts_User)
            pcbnew.Refresh()
        except Exception:
            pass
        wx.CallLater(120, self._clear_pcb_selection)

    def _goto_pcb_ref(self, ref: str, mark_msg: "Optional[str]" = None) -> None:
        """Center the PCB editor on the footprint with the given reference designator."""
        try:
            import pcbnew
            board = pcbnew.GetBoard()
            if board is None:
                return
            for fp in board.GetFootprints():
                if fp.GetReference() == ref:
                    layer = fp.GetLayer()
                    try:
                        pcbnew.FocusOnItem(fp, layer)
                    except Exception:
                        try:
                            pcbnew.FocusOnItem(fp)
                        except Exception:
                            pass
                    if mark_msg:
                        self._place_pcb_marker(fp.GetPosition(), mark_msg, board)
                    pcbnew.Refresh()
                    # Defer layer switch so FocusOnItem's internal refresh doesn't override it
                    wx.CallLater(80, self._set_active_layer, layer)
                    break
        except Exception:
            pass
        wx.CallLater(120, self._clear_pcb_selection)

    def _clear_pcb_selection(self) -> None:
        """Deselect everything in the PCB editor after a navigation jump."""
        try:
            import pcbnew
            board = pcbnew.GetBoard()
            if board is None:
                return
            frame = board.GetParent()
            if frame is None:
                return
            try:
                tm = frame.GetToolManager()
                if tm:
                    tm.RunAction("common.Interactive.clearSelection", True)
            except Exception:
                pass
            pcbnew.Refresh()
        except Exception:
            pass

    def _goto_sch_ref(self, ref: str) -> None:
        """Navigate to a component's footprint in the PCB editor."""
        self._goto_pcb_ref(ref)

    def _place_pcb_marker(self, pos, message: str, board=None):
        """Place a PCB_TEXT annotation on the Comments layer at pos. Returns the text item."""
        try:
            import pcbnew
            if board is None:
                board = pcbnew.GetBoard()
            if board is None:
                return None
            text = pcbnew.PCB_TEXT(board)
            text.SetText("\u26a0 " + message[:60])
            text.SetPosition(pos)
            text.SetLayer(pcbnew.Cmts_User)
            try:
                text.SetTextSize(pcbnew.VECTOR2I(
                    int(pcbnew.FromMM(1.5)), int(pcbnew.FromMM(1.5))
                ))
            except Exception:
                try:
                    text.SetTextHeight(int(pcbnew.FromMM(1.5)))
                    text.SetTextWidth(int(pcbnew.FromMM(1.5)))
                except Exception:
                    pass
            board.Add(text)
            pcbnew.Refresh()
            return text
        except Exception:
            return None

    def _edit_message(self, index: int) -> None:
        if self._is_busy:
            return
        item = self.history[index]
        if str(item.get("role", "")) != "user":
            return

        dialog = wx.TextEntryDialog(
            self,
            "Modifie ton message puis renvoie-le :",
            "Éditer le message",
            str(item.get("content", "")),
            style=wx.OK | wx.CANCEL | wx.TE_MULTILINE,
        )
        dialog.SetSize((560, 320))
        try:
            if dialog.ShowModal() == wx.ID_OK:
                new_text = dialog.GetValue().strip()
                if new_text:
                    self._resubmit_edited_user_message(index, new_text)
        finally:
            dialog.Destroy()

    # ------------------------------------------------------------------
    # Cost accounting
    # ------------------------------------------------------------------
    def _extract_cost_stats(self, response: Dict[str, Any]) -> Dict[str, Any]:
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        if not isinstance(usage, dict):
            usage = {}

        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)

        model = str(response.get("model", "") or "")
        if not model:
            raw = response.get("raw", {}) if isinstance(response, dict) else {}
            if isinstance(raw, dict):
                model = str(raw.get("model", "") or "")

        pricing = _model_pricing(model)
        prompt_cost = (prompt_tokens * pricing.get("input", 0.0)) / 1_000_000.0
        completion_cost = (completion_tokens * pricing.get("output", 0.0)) / 1_000_000.0
        total_cost = prompt_cost + completion_cost

        return {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "prompt_cost_usd": round(prompt_cost, 6),
            "completion_cost_usd": round(completion_cost, 6),
            "total_cost_usd": round(total_cost, 6),
        }

    def _assistant_message_from_response(self, content: str, response: Dict[str, Any]) -> Dict[str, Any]:
        item: Dict[str, Any] = {"role": "assistant", "content": content}
        stats = self._extract_cost_stats(response)
        item.update(stats)
        return item

    def _compute_spend_usd(self) -> float:
        if not self._spend_total_dirty:
            return self._spend_total_usd

        total = 0.0
        if not os.path.isdir(self.chats_root_dir):
            self._spend_total_usd = total
            self._spend_total_dirty = False
            return total

        for name in os.listdir(self.chats_root_dir):
            history_path = self._session_history_path(name)
            history = self._read_json(history_path, [])
            if not isinstance(history, list):
                continue
            for item in history:
                if not isinstance(item, dict):
                    continue
                try:
                    total += float(item.get("total_cost_usd", 0.0) or 0.0)
                except Exception:
                    continue
        self._spend_total_usd = total
        self._spend_total_dirty = False
        return total

    def _update_header_stats(self) -> None:
        title = str(self.current_session_meta.get("title", "New chat")) or "New chat"
        self.title_label.SetLabel(title)
        self.title_label.SetToolTip(f"Context: {self.current_session_dir or '(none)'}")

        cfg = self._settings.load_runtime_config()
        spend = self._compute_spend_usd()

        try:
            budget_total = float(cfg.get("monthly_budget_usd", 0.0) or 0.0)
        except Exception:
            budget_total = 0.0

        if budget_total > 0.0:
            remaining = max(0.0, budget_total - spend)
            self.budget_info.SetLabel(f"Remaining budget: ${remaining:.3f}  ·  spent ${spend:.3f}")
        else:
            self.budget_info.SetLabel(f"Estimated used budget : ${spend:.3f}")

        self.title_label.GetParent().Layout()

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------
    def _session_dir(self, session_id: str) -> str:
        return os.path.join(self.chats_root_dir, session_id)

    def _session_meta_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "session.json")

    def _session_history_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "history.json")

    def _read_json(self, path: str, default: Any) -> Any:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as stream:
                return json.load(stream)
        except Exception:
            return default

    def _write_json(self, path: str, payload: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)

    def _list_sessions(self) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.chats_root_dir):
            return []

        sessions: List[Dict[str, Any]] = []
        for name in os.listdir(self.chats_root_dir):
            session_id = name.strip()
            if not session_id:
                continue
            meta = self._read_json(self._session_meta_path(session_id), {})
            if not isinstance(meta, dict):
                continue
            sessions.append(
                {
                    "id": session_id,
                    "title": str(meta.get("title", "New chat")),
                    "updated_at": str(meta.get("updated_at", "")),
                }
            )

        sessions.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return sessions

    def _create_session(self, title: str) -> str:
        session_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        created_at = _utc_now_iso()
        meta = {
            "id": session_id,
            "title": title or "New chat",
            "created_at": created_at,
            "updated_at": created_at,
        }
        self._write_json(self._session_meta_path(session_id), meta)
        self._write_json(self._session_history_path(session_id), [])
        self._spend_total_dirty = True
        return session_id

    def _refresh_chat_list(self) -> None:
        self._session_items = self._list_sessions()
        self.chat_list.Clear()
        for item in self._session_items:
            self.chat_list.Append(str(item.get("title", "New chat")))

    def _select_session_by_id(self, session_id: str) -> None:
        for index, item in enumerate(self._session_items):
            if str(item.get("id", "")) == session_id:
                self.chat_list.SetSelection(index)
                self._load_session(session_id)
                return

    def _load_session(self, session_id: str) -> None:
        meta = self._read_json(self._session_meta_path(session_id), {})
        history = self._read_json(self._session_history_path(session_id), [])
        if not isinstance(meta, dict):
            return
        if not isinstance(history, list):
            history = []

        self.current_session_id = session_id
        self.current_session_dir = self._session_dir(session_id)
        self.current_session_meta = meta
        self.history = history
        self._render_history()

    def _save_current_session(self) -> None:
        if not self.current_session_id:
            return
        meta = dict(self.current_session_meta) if isinstance(self.current_session_meta, dict) else {}
        meta.setdefault("id", self.current_session_id)
        meta.setdefault("title", "New chat")
        meta.setdefault("created_at", _utc_now_iso())
        meta["updated_at"] = _utc_now_iso()
        self.current_session_meta = meta

        self._write_json(self._session_meta_path(self.current_session_id), meta)
        self._write_json(self._session_history_path(self.current_session_id), self.history)
        self._spend_total_dirty = True

    def _init_chat_sessions(self) -> None:
        os.makedirs(self.chats_root_dir, exist_ok=True)
        sessions = self._list_sessions()
        if not sessions:
            active_id = self._create_session("New chat")
        else:
            active_id = str(sessions[0].get("id", ""))

        self._refresh_chat_list()
        if active_id:
            self._select_session_by_id(active_id)

    # ------------------------------------------------------------------
    # AI turn handling
    # ------------------------------------------------------------------
    def _update_typing_status(self, message: str) -> None:
        """Update the status text inside the typing bubble via JS (no full re-render)."""
        if not self._is_busy:
            return
        safe = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
        try:
            self.webview.RunScript(
                "var el=document.getElementById('ai-status');if(el)el.textContent='" + safe + "';"
            )
        except Exception:
            pass

    def _append_stream_chunk(self, text: str) -> None:
        """Accumulate a streaming chunk and schedule a batched WebView update."""
        if not self._is_busy:
            return
        # Strip bare tool tokens — they are intercepted later and must not be shown.
        clean = COMMAND_PATTERN.sub("", text)
        self._stream_content += clean
        if not self._stream_pending:
            self._stream_pending = True
            wx.CallLater(30, self._flush_stream_to_webview)

    def _flush_stream_to_webview(self) -> None:
        """Push accumulated stream content into #stream-content, hiding the dots."""
        self._stream_pending = False
        if not self._is_busy:
            return
        # Guard: if content was cleared by a tool call between scheduling and firing,
        # do nothing — avoids showing an empty bubble with the dots hidden.
        if not self._stream_content:
            return
        import json as _json
        rendered_html = markdown_to_html(self._stream_content)
        escaped = _json.dumps(rendered_html)  # proper JS string literal
        try:
            self.webview.RunScript(
                "(function(){"
                "var ta=document.getElementById('thinking-area');"
                "var sc=document.getElementById('stream-content');"
                "if(!sc)return;"
                "if(sc.style.display==='none'){"
                "  if(ta)ta.style.display='none';"
                "  sc.style.display='block';"
                "}"
                "sc.innerHTML=" + escaped + ";"
                "})()"
            )
        except Exception:
            pass

    def _on_thinking_chunk(self, text: str) -> None:
        """Show the last non-empty line of a thinking chunk in the status span."""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if lines:
            self._update_typing_status(lines[-1][:120])

    def _clear_stream_for_tool(self) -> None:
        """Reset the streaming area when a tool call is detected mid-stream."""
        self._stream_content = ""
        self._stream_pending = False
        try:
            self.webview.RunScript(
                "(function(){"
                "var ta=document.getElementById('thinking-area');"
                "var sc=document.getElementById('stream-content');"
                "if(ta)ta.style.display='flex';"
                "if(sc){sc.style.display='none';sc.innerText='';}"
                "})()"
            )
        except Exception:
            pass

    def _set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        self.btn_send.Enable(not busy)
        self.btn_settings.Enable(not busy)
        self.btn_marker.Enable(not busy)
        self.btn_new_chat.Enable(not busy)
        self.btn_delete_chat.Enable(not busy)
        self.chat_list.Enable(not busy)
        self.input.Enable(not busy)

    def _extract_command_from_text(self, text: str) -> str:
        match = COMMAND_PATTERN.search(text)
        return match.group(1).lower() if match else ""

    def _run_on_main(self, fn, *args, **kwargs) -> Any:
        """Execute fn(*args, **kwargs) on the wx/KiCad main thread.

        Blocks the calling (background) thread until the main thread
        completes the call and returns the result (or re-raises exceptions).
        Required for ALL pcbnew API operations — calling them from a
        background thread crashes KiCad.
        """
        done_event = threading.Event()
        result_box: List[Any] = [None]
        error_box:  List[Any] = [None]

        def _dispatch():
            try:
                result_box[0] = fn(*args, **kwargs)
            except Exception as _exc:
                error_box[0] = _exc
            finally:
                done_event.set()

        wx.CallAfter(_dispatch)
        done_event.wait()

        if error_box[0] is not None:
            raise error_box[0]
        return result_box[0]

    def _compute_ai_turn_messages(
        self,
        session_dir: str,
        history_snapshot: List[Dict[str, str]],
        on_status=None,
        on_chunk=None,
        on_thinking=None,
        on_tool_start=None,
    ) -> List[Dict[str, Any]]:
        new_messages: List[Dict[str, Any]] = []

        _TOOL_STATUS = {
            "execute_drc":               "Running DRC…",
            "run_drc":                   "Running DRC…",
            "execute_erc":               "Running ERC…",
            "run_erc":                   "Running ERC…",
            "execute_full":              "Full project analysis…",
            "read_project":              "Loading project…",
            "read_pcb":                  "Loading PCB…",
            "export_pdf":                "Exporting PDF layers…",
            "export_images":             "Exporting layer images…",
            "read_design_rules":         "Reading design rules…",
            "read_net_class_assignments":"Reading net class assignments…",
            "ultralibrarian_search":     "Searching UltraLibrarian…",
            "ultralibrarian_get_part":   "Opening UltraLibrarian part…",
            "ultralibrarian_lookup":     "Resolving UltraLibrarian part…",
            "modify_netclass":           "Modifying net class…",
            "set_drc_severity":          "Updating DRC severity…",
            "save_board":                "Saving board…",
        }

        response = ask_ai(
            session_dir, history_snapshot,
            board_file=self.board_file,
            on_status=on_status, on_chunk=on_chunk, on_thinking=on_thinking,
        )
        if not response.get("ok", False):
            details = response.get("details")
            msg = response.get("message", "Erreur inconnue.")
            new_messages.append({"role": "error", "content": msg, "details": str(details or "")})
            return new_messages

        max_tool_rounds = 6  # increased to allow read→write→save chains
        temp_history = list(history_snapshot)
        _collected_layer_images: Dict[str, str] = {}  # layer_name → file path

        for _ in range(max_tool_rounds):

            # ── Case 1: Function-calling (tool_calls response) ───────────────
            if response.get("tool_calls"):
                if on_tool_start:
                    on_tool_start()

                tool_calls = response["tool_calls"]
                # Add assistant message with tool_calls so the API knows what was requested
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": response.get("content") or "",
                    "tool_calls": tool_calls,
                    "_hidden": True,
                }
                new_messages.append(assistant_msg)
                temp_history = temp_history + [assistant_msg]

                tool_result_messages: List[Dict[str, Any]] = []
                accumulated_image_b64: Dict[str, str] = {}

                for tc in tool_calls:
                    func_name = str(tc.get("function", {}).get("name", ""))
                    try:
                        func_args = json.loads(tc.get("function", {}).get("arguments") or "{}")
                    except Exception:
                        func_args = {}

                    if on_status:
                        on_status(_TOOL_STATUS.get(func_name, f"Running {func_name}…"))

                    action_result = self._run_on_main(
                        execute_action, func_name, self.board_file, session_dir, func_args)

                    # Collect layer images for display
                    if func_name == "export_images":
                        for p in action_result.get("image_paths", []):
                            layer_name = os.path.basename(p).replace(".png", "").replace("_", ".")
                            _collected_layer_images[layer_name] = p
                        accumulated_image_b64.update(action_result.get("image_b64", {}))

                    tool_content = action_result.get("data", "").strip() or action_result.get("message", "Done.")
                    tool_msg: Dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": tool_content,
                        "_hidden": True,
                    }
                    tool_result_messages.append(tool_msg)
                    new_messages.append(tool_msg)

                temp_history = temp_history + tool_result_messages

                # Vision: if export_images was called, attach images to the follow-up
                image_b64 = accumulated_image_b64
                followup_history = list(temp_history)
                if image_b64:
                    content_parts: List[Any] = []
                    for _ln, b64_data in list(image_b64.items())[:6]:
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,{}".format(b64_data),
                                "detail": "low",
                            },
                        })
                    followup_history.append({"role": "user", "content": content_parts})
                    followup_history.append({
                        "role": "system",
                        "content": "Tool completed. Resume your answer in the user's language. Do not mention the images.",
                    })
                else:
                    followup_history.append({
                        "role": "system",
                        "content": "Tool completed. Resume your answer to the user's question in their language.",
                    })

                response = ask_ai(
                    session_dir, followup_history,
                    board_file=self.board_file,
                    on_status=on_status, on_chunk=on_chunk, on_thinking=on_thinking,
                )
                if not response.get("ok", False):
                    details = response.get("details")
                    msg = response.get("message", "Erreur inconnue.")
                    new_messages.append({"role": "error", "content": msg, "details": str(details or "")})
                    return new_messages
                continue  # check the next response (may have more tool calls)

            # ── Case 2: Legacy text-token approach (fallback for non-function-calling models) ──
            current_content = str(response.get("content", "")).strip() or "(empty response)"
            command = self._extract_command_from_text(current_content)

            if not command:
                # Final answer — no more tools
                clean_content = COMMAND_PATTERN.sub("", current_content).strip() or "(empty response)"
                msg = self._assistant_message_from_response(clean_content, response)
                if _collected_layer_images:
                    msg["layer_images"] = dict(_collected_layer_images)
                new_messages.append(msg)
                return new_messages

            # Legacy token detected
            new_messages.append(self._assistant_message_from_response(current_content, response))
            new_messages[-1]["_hidden"] = True
            new_messages.append({"role": "system", "content": f"Requested command: {{{command}}}", "_hidden": True})

            if on_tool_start:
                on_tool_start()
            if on_status:
                on_status(_TOOL_STATUS.get(command, "Running tool…"))

            action_result = self._run_on_main(
                execute_action, command, self.board_file, session_dir)
            result_data = action_result.get("data", "").strip()
            if not result_data:
                result_data = self._format_action_result(action_result)
            new_messages.append({"role": "system", "content": "Tool result:\n" + result_data, "_hidden": True})
            new_messages.append({"role": "system", "content": "Tool executed, resuming analysis.", "_hidden": True})

            if command == "export_images":
                for p in action_result.get("image_paths", []):
                    layer_name = os.path.basename(p).replace(".png", "").replace("_", ".")
                    _collected_layer_images[layer_name] = p

            image_b64 = action_result.get("image_b64", {})
            temp_history = temp_history + new_messages
            followup_history = list(temp_history)
            if image_b64:
                content_parts = []
                for _ln, b64_data in list(image_b64.items())[:6]:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,{}".format(b64_data),
                            "detail": "low",
                        },
                    })
                followup_history.append({"role": "user", "content": content_parts})
                followup_history.append({
                    "role": "system",
                    "content": "Tool completed. Resume your answer to the user's question in their language. Do not mention or confirm the images.",
                })
            else:
                followup_history.append({
                    "role": "system",
                    "content": "Tool completed. Resume your answer to the user's question in their language.",
                })

            followup = ask_ai(
                session_dir, followup_history,
                board_file=self.board_file,
                on_status=on_status, on_chunk=on_chunk, on_thinking=on_thinking,
            )
            if not followup.get("ok", False):
                details = followup.get("details")
                msg = followup.get("message", "Erreur inconnue.")
                new_messages.append({"role": "error", "content": msg, "details": str(details or "")})
                return new_messages

            response = followup

        # Loop exhausted
        current_content = str(response.get("content", "")).strip()
        current_content = COMMAND_PATTERN.sub("", current_content).strip()
        if not current_content:
            current_content = "(L'opération a échoué après plusieurs tentatives.)"
        msg = self._assistant_message_from_response(current_content, response)
        if _collected_layer_images:
            msg["layer_images"] = dict(_collected_layer_images)
        new_messages.append(msg)
        return new_messages

    def _start_async_ai_turn(self) -> None:
        if self._is_busy:
            return
        if not self.current_session_id:
            return

        self._set_busy(True)
        self._stream_content = ""
        self._stream_pending = False
        self._render_history()  # shows the animated typing bubble

        session_id = self.current_session_id
        session_dir = self.current_session_dir
        history_snapshot = list(self.history)

        def on_status(msg: str) -> None:
            wx.CallAfter(self._update_typing_status, msg)

        def on_chunk(text: str) -> None:
            wx.CallAfter(self._append_stream_chunk, text)

        def on_thinking(text: str) -> None:
            wx.CallAfter(self._on_thinking_chunk, text)

        def on_tool_start() -> None:
            wx.CallAfter(self._clear_stream_for_tool)

        def _worker() -> None:
            try:
                new_messages = self._compute_ai_turn_messages(
                    session_dir, history_snapshot,
                    on_status=on_status,
                    on_chunk=on_chunk,
                    on_thinking=on_thinking,
                    on_tool_start=on_tool_start,
                )
            except Exception as exc:  # noqa: BLE001
                error_msg = f"Unexpected error: {exc}"
                new_messages = [{"role": "error", "content": error_msg, "details": ""}]
            wx.CallAfter(self._finish_async_ai_turn, session_id, new_messages)

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_async_ai_turn(self, session_id: str, new_messages: List[Dict[str, Any]]) -> None:
        self._set_busy(False)

        if session_id != self.current_session_id:
            return

        self.history.extend(new_messages)
        self._maybe_set_ai_title()
        self._save_current_session()
        self._render_history()

    def _copy_to_clipboard(self, text: str) -> None:
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()

    def _format_error_card(self, title: str, details: str) -> str:
        parsed_message = ""
        parsed_type = ""
        parsed_param = ""
        parsed_code = ""

        try:
            parsed = json.loads(details) if details else {}
            err = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            if isinstance(err, dict):
                parsed_message = str(err.get("message", ""))
                parsed_type = str(err.get("type", ""))
                parsed_param = str(err.get("param", ""))
                parsed_code = str(err.get("code", ""))
        except Exception:
            pass

        lines = [f"**{title}**"]
        if parsed_message:
            lines.append(f"**Message:** {parsed_message}")
        if parsed_type:
            lines.append(f"**Type:** `{parsed_type}`")
        if parsed_param:
            lines.append(f"**Param:** `{parsed_param}`")
        if parsed_code:
            lines.append(f"**Code:** `{parsed_code}`")
        if details and not parsed_message:
            lines.append("**Details:**")
            lines.append(details)
        lines.append("_Hint: try a different model in Settings if this option is not supported._")
        return "\n".join(lines)

    def _maybe_set_ai_title(self) -> None:
        title = str(self.current_session_meta.get("title", "New chat"))
        if title != "New chat":
            return

        if len(self.history) < 2:
            return

        ai_title = suggest_chat_title(self.current_session_dir, self.history)
        ai_title = " ".join(ai_title.split()).strip()
        if not ai_title:
            return

        self.current_session_meta["title"] = ai_title[:50]
        self._save_current_session()
        self._refresh_chat_list()
        self._select_session_by_id(self.current_session_id)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_input_key(self, event: wx.KeyEvent) -> None:
        key_code = event.GetKeyCode()
        # Cmd/Ctrl+A → sélectionner uniquement le contenu de l'input
        if key_code == ord('A') and (event.CmdDown() or event.ControlDown()):
            self.input.SelectAll()
            return
        if key_code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if event.ShiftDown() or event.CmdDown() or event.ControlDown():
                self.input.AddText("\n")
            else:
                self._on_send(None)
            return
        event.Skip()

    def _on_settings(self, _event: Any) -> None:
        if self._is_busy:
            return
        saved = open_settings_dialog(self)
        if saved:
            self._update_header_stats()

    def _on_send(self, _event: Any) -> None:
        if self._is_busy:
            return
        if self._placeholder_active:
            return
        message = self.input.GetText().strip()
        if not message:
            return

        if not self.current_session_dir:
            return

        os.makedirs(self.current_session_dir, exist_ok=True)

        self.input.SetText("")
        self.history.append({"role": "user", "content": message})
        self._save_current_session()
        self._render_history()
        self._start_async_ai_turn()

    def _resubmit_edited_user_message(self, history_index: int, new_text: str) -> None:
        if history_index < 0 or history_index >= len(self.history):
            return

        item = self.history[history_index]
        if str(item.get("role", "")) != "user":
            return

        self.history = self.history[:history_index]
        self.history.append({"role": "user", "content": new_text})
        self._save_current_session()
        self._render_history()
        self._start_async_ai_turn()

    def _format_action_result(self, payload: Dict[str, Any]) -> str:
        action = payload.get("action", "")
        ok = payload.get("ok", False)
        message = payload.get("message", "")
        issue_count = payload.get("issue_count", None)
        returncode = payload.get("returncode", None)
        details = [f"action={action}", f"ok={ok}", f"message={message}"]
        if isinstance(issue_count, int):
            details.append(f"issue_count={issue_count}")
        if isinstance(returncode, int):
            details.append(f"returncode={returncode}")
        stderr = payload.get("stderr", "")
        if stderr:
            details.append(f"stderr={stderr}")
        return "; ".join(details)

    def _on_new_chat(self, _event: Any) -> None:
        if self._is_busy:
            return
        session_id = self._create_session("New chat")
        self._refresh_chat_list()
        self._select_session_by_id(session_id)

    def _on_delete_chat(self, _event: Any) -> None:
        if self._is_busy:
            return
        if not self.current_session_id:
            return

        if self.chat_list.GetCount() <= 1:
            wx.MessageBox("Cannot delete the last conversation.", "Maelectrix",
                          wx.OK | wx.ICON_INFORMATION)
            return

        confirm = wx.MessageBox(
            "Delete this conversation and its local context?",
            "Confirm deletion",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if confirm != wx.YES:
            return

        session_dir = self._session_dir(self.current_session_id)
        if os.path.isdir(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        self._spend_total_dirty = True

        self._refresh_chat_list()
        if self._session_items:
            first_id = str(self._session_items[0].get("id", ""))
            if first_id:
                self._select_session_by_id(first_id)

    def _on_select_chat(self, _event: wx.CommandEvent) -> None:
        index = self.chat_list.GetSelection()
        if index < 0 or index >= len(self._session_items):
            return

        session_id = str(self._session_items[index].get("id", ""))
        if session_id and session_id != self.current_session_id:
            self._load_session(session_id)

    def _on_marker(self, _event: Any) -> None:
        if self._is_busy:
            return
        if not self.board_file:
            wx.MessageBox("Fichier board introuvable.", "Capture", wx.OK | wx.ICON_WARNING)
            return
        if not self.current_session_dir:
            return

        x_text = wx.GetTextFromUser("Position X en pourcentage (0-100)", "Marker", "50")
        if not x_text:
            return
        y_text = wx.GetTextFromUser("Position Y en pourcentage (0-100)", "Marker", "50")
        if not y_text:
            return
        label = wx.GetTextFromUser("Label du marker", "Marker", "Issue")
        if not label:
            label = "Issue"

        try:
            x_percent = float(x_text)
            y_percent = float(y_text)
        except Exception:
            wx.MessageBox("Valeurs invalides pour X/Y.", "Capture", wx.OK | wx.ICON_WARNING)
            return

        exported = export_board_svg(self.board_file, self.current_session_dir)
        if not exported.get("ok", False):
            wx.MessageBox(f"Export SVG impossible : {exported.get('message', 'unknown error')}",
                          "Capture", wx.OK | wx.ICON_ERROR)
            return

        marked = add_svg_marker(str(exported.get("svg_path", "")), x_percent, y_percent, label)
        if not marked.get("ok", False):
            wx.MessageBox(f"Annotation impossible : {marked.get('message', 'unknown error')}",
                          "Capture", wx.OK | wx.ICON_ERROR)
            return

        path = str(marked.get("annotated_svg", ""))
        self.history.append({"role": "system", "content": f"Marker ajouté : `{path}`"})
        self._save_current_session()
        self._render_history()


class ChatWindow:
    @staticmethod
    def show(board_file: str, context_dir: str) -> None:
        frame = ChatFrame(board_file=board_file, context_dir=context_dir)
        frame.Show()
