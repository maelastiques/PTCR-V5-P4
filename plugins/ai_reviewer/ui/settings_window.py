"""Modern settings dialog for the Maelectrix plugin."""

from __future__ import annotations

from typing import Any, Dict, Optional

import wx

from .markdown_render import build_theme
from .settings import PluginSettings
from .widgets import FlatButton, RoundedPanel, hex_to_colour, is_dark_colour


OPENAI_MODELS = [
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
    "o3-mini",
    "o4-mini",
]


class SettingsDialog(wx.Dialog):
    def __init__(self, parent: Optional[wx.Window] = None) -> None:
        super().__init__(parent, title="Settings — Maelectrix", style=wx.DEFAULT_DIALOG_STYLE)

        self._settings = PluginSettings()
        self._config = self._settings.load_runtime_config()

        self._is_dark = is_dark_colour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
        self.theme = build_theme(self._is_dark)
        self.c_app = hex_to_colour(self.theme["app_bg"])
        self.c_surface = hex_to_colour(self.theme["surface"])
        self.c_text = hex_to_colour(self.theme["text"])
        self.c_muted = hex_to_colour(self.theme["muted"])
        self.c_accent = hex_to_colour(self.theme["accent"])
        self.c_border = hex_to_colour(self.theme["border"])

        self.SetBackgroundColour(self.c_app)

        root = wx.BoxSizer(wx.VERTICAL)

        # ---- Header ------------------------------------------------------
        header = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(self, label="Settings")
        title_font = title.GetFont()
        title_font.SetPointSize(title_font.GetPointSize() + 6)
        title_font.MakeBold()
        title.SetFont(title_font)
        title.SetForegroundColour(self.c_text)
        header.Add(title, 0)

        subtitle = wx.StaticText(self, label="OpenAI Copilot Configuration")
        subtitle.SetForegroundColour(self.c_muted)
        header.Add(subtitle, 0, wx.TOP, 3)
        root.Add(header, 0, wx.LEFT | wx.RIGHT | wx.TOP, 26)

        # ---- API key card ------------------------------------------------
        key_card, key_inner = self._make_card("OPENAI API KEY")
        self.api_key = wx.TextCtrl(
            key_card,
            value=str(self._config.get("openai_api_key", "")),
            style=wx.TE_PASSWORD,
        )
        self._style_input(self.api_key)
        key_inner.Add(self.api_key, 0, wx.EXPAND)

        self.show_key = wx.CheckBox(key_card, label="Show key")
        self.show_key.SetForegroundColour(self.c_muted)
        self.show_key.SetBackgroundColour(self.c_surface)
        self.show_key.Bind(wx.EVT_CHECKBOX, self._on_toggle_key_visibility)
        key_inner.Add(self.show_key, 0, wx.TOP, 10)

        hint = wx.StaticText(
            key_card,
            label="The key is stored locally and never shared.",
        )
        hint.SetForegroundColour(self.c_muted)
        hint_font = hint.GetFont()
        hint_font.SetPointSize(max(9, hint_font.GetPointSize() - 1))
        hint.SetFont(hint_font)
        key_inner.Add(hint, 0, wx.TOP, 8)

        root.Add(key_card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 26)

        # ---- Model & options card ---------------------------------------
        opt_card, opt_inner = self._make_card("MODEL & OPTIONS")
        form = wx.FlexGridSizer(cols=2, hgap=14, vgap=12)
        form.AddGrowableCol(1, 1)

        form.Add(self._field_label(opt_card, "Provider"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.provider = wx.TextCtrl(opt_card, value=str(self._config.get("provider", "openai")))
        self._style_input(self.provider)
        form.Add(self.provider, 1, wx.EXPAND)

        form.Add(self._field_label(opt_card, "Model"), 0, wx.ALIGN_CENTER_VERTICAL)
        configured_model = str(self._config.get("openai_model", "gpt-4.1-mini")).strip()
        model_choices = list(OPENAI_MODELS)
        if configured_model and configured_model not in model_choices:
            model_choices.append(configured_model)
        self.model = wx.Choice(opt_card, choices=model_choices)
        self.model.SetBackgroundColour(self.c_surface)
        self.model.SetForegroundColour(self.c_text)
        if configured_model in model_choices:
            self.model.SetSelection(model_choices.index(configured_model))
        else:
            self.model.SetSelection(model_choices.index("gpt-4.1-mini"))
        form.Add(self.model, 1, wx.EXPAND)

        form.Add(self._field_label(opt_card, "Temperature"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.temperature = wx.TextCtrl(opt_card, value=str(self._config.get("temperature", 0.2)))
        self._style_input(self.temperature)
        form.Add(self.temperature, 1, wx.EXPAND)

        opt_inner.Add(form, 0, wx.EXPAND)
        root.Add(opt_card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 26)

        # ---- Footer buttons ---------------------------------------------
        footer = wx.BoxSizer(wx.HORIZONTAL)
        footer.AddStretchSpacer(1)
        btn_cancel = FlatButton(
            self,
            "Cancel",
            self.c_surface,
            self.c_text,
            lambda _e: self.EndModal(wx.ID_CANCEL),
            radius=10,
            bold=False,
            min_size=(110, 40),
            border=self.c_border,
        )
        footer.Add(btn_cancel, 0, wx.RIGHT, 10)
        btn_save = FlatButton(
            self,
            "Save",
            self.c_accent,
            wx.Colour(255, 255, 255),
            lambda _e: self.EndModal(wx.ID_OK),
            radius=10,
            min_size=(140, 40),
        )
        footer.Add(btn_save, 0)
        root.Add(footer, 0, wx.EXPAND | wx.ALL, 26)

        self.SetSizer(root)
        self.Layout()
        self.SetMinSize((560, 100))
        self.Fit()
        self.CentreOnParent()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def _make_card(self, heading: str):
        """Return (card_panel, inner_sizer) for a titled rounded section."""
        card = RoundedPanel(self, self.c_surface, self.c_border, radius=14)
        outer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(card, label=heading)
        label.SetForegroundColour(self.c_muted)
        heading_font = label.GetFont()
        heading_font.SetPointSize(max(9, heading_font.GetPointSize() - 1))
        heading_font.MakeBold()
        label.SetFont(heading_font)
        outer.Add(label, 0, wx.BOTTOM, 12)

        inner = wx.BoxSizer(wx.VERTICAL)
        outer.Add(inner, 1, wx.EXPAND)

        pad = wx.BoxSizer(wx.VERTICAL)
        pad.Add(outer, 1, wx.EXPAND | wx.ALL, 18)
        card.SetSizer(pad)
        return card, inner

    def _field_label(self, parent: wx.Window, text: str) -> wx.StaticText:
        label = wx.StaticText(parent, label=text)
        label.SetForegroundColour(self.c_text)
        return label

    def _style_input(self, ctrl: wx.TextCtrl) -> None:
        ctrl.SetBackgroundColour(self.c_surface if self._is_dark else wx.Colour(255, 255, 255))
        ctrl.SetForegroundColour(self.c_text)
        ctrl.SetMinSize((-1, 30))

    def _on_toggle_key_visibility(self, _event: wx.CommandEvent) -> None:
        value = self.api_key.GetValue()
        parent = self.api_key.GetParent()
        sizer = self.api_key.GetContainingSizer()
        if not parent or not sizer:
            return

        index = 0
        for i, child in enumerate(sizer.GetChildren()):
            if child.GetWindow() is self.api_key:
                index = i
                break

        style = 0 if self.show_key.GetValue() else wx.TE_PASSWORD
        self.api_key.Destroy()
        self.api_key = wx.TextCtrl(parent, value=value, style=style)
        self._style_input(self.api_key)
        sizer.Insert(index, self.api_key, 0, wx.EXPAND)
        parent.Layout()

    # ------------------------------------------------------------------
    # Public API (contract preserved)
    # ------------------------------------------------------------------
    def get_config(self) -> Dict[str, Any]:
        try:
            temperature = float(self.temperature.GetValue().strip())
        except Exception:
            temperature = 0.2

        return {
            "provider": self.provider.GetValue().strip() or "openai",
            "openai_model": self.model.GetStringSelection().strip() or "gpt-4.1-mini",
            "openai_api_key": self.api_key.GetValue().strip(),
            "temperature": temperature,
        }


def open_settings_dialog(parent: Optional[wx.Window] = None) -> bool:
    settings = PluginSettings()
    dialog = SettingsDialog(parent)
    try:
        if dialog.ShowModal() == wx.ID_OK:
            config = dialog.get_config()
            settings.save_runtime_config(config)
            return True
        return False
    finally:
        dialog.Destroy()
