"""Shared themed wx widgets used across the Maelectrix UI."""

from __future__ import annotations

from typing import Any, Optional

import wx


def is_dark_colour(colour: wx.Colour) -> bool:
    """Relative-luminance heuristic used for light/dark theme adaptation."""
    luminance = (0.2126 * colour.Red()) + (0.7152 * colour.Green()) + (0.0722 * colour.Blue())
    return luminance < 128


def hex_to_colour(value: str) -> wx.Colour:
    value = value.lstrip("#")
    if len(value) == 6:
        return wx.Colour(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    return wx.Colour(value)


def shade(colour: wx.Colour, factor: float) -> wx.Colour:
    """Lighten (factor>1) or darken (factor<1) a colour, clamped to 0..255."""
    def _clamp(component: float) -> int:
        return max(0, min(255, int(component)))

    return wx.Colour(
        _clamp(colour.Red() * factor),
        _clamp(colour.Green() * factor),
        _clamp(colour.Blue() * factor),
        colour.Alpha(),
    )


class FlatButton(wx.Panel):
    """A themed, owner-drawn button with hover/pressed/disabled states.

    Native wx.Button ignores SetBackgroundColour on macOS, so a custom
    control is used to guarantee a consistent modern look across platforms.
    """

    def __init__(
        self,
        parent: wx.Window,
        label: str,
        bg: wx.Colour,
        fg: wx.Colour,
        on_click: Any = None,
        *,
        radius: int = 11,
        bold: bool = True,
        min_size: tuple = (-1, 38),
        flat: bool = False,
        border: Optional[wx.Colour] = None,
    ) -> None:
        super().__init__(parent, style=wx.BORDER_NONE)
        self._label = label
        self._bg = bg
        self._fg = fg
        self._radius = radius
        self._flat = flat
        self._border = border
        self._on_click = on_click
        self._hover = False
        self._enabled = True

        self.SetBackgroundColour(parent.GetBackgroundColour())
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize(min_size)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

        font = self.GetFont()
        if bold:
            font.MakeBold()
        self.SetFont(font)

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda _e: self.Refresh())
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)

    def SetLabel(self, label: str) -> None:  # noqa: N802 (wx naming)
        self._label = label
        self.Refresh()

    def Enable(self, enable: bool = True) -> bool:  # noqa: N802 (wx naming)
        self._enabled = enable
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if enable else wx.CURSOR_ARROW))
        self.Refresh()
        return super().Enable(enable)

    def _on_enter(self, _event: wx.MouseEvent) -> None:
        self._hover = True
        self.Refresh()

    def _on_leave(self, _event: wx.MouseEvent) -> None:
        self._hover = False
        self.Refresh()

    def _on_left_up(self, event: wx.MouseEvent) -> None:
        if self._enabled and self._on_click is not None:
            self._on_click(event)

    def _on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        if gc is None:
            return

        width, height = self.GetClientSize()
        if width <= 1 or height <= 1:
            return

        if not self._enabled:
            bg = shade(self._bg, 0.85 if is_dark_colour(self._bg) else 1.08)
            fg = wx.Colour(self._fg.Red(), self._fg.Green(), self._fg.Blue(), 110)
        elif self._hover:
            bg = shade(self._bg, 1.12 if is_dark_colour(self._bg) else 0.94)
            fg = self._fg
        else:
            bg = self._bg
            fg = self._fg

        if self._flat and not self._hover and self._enabled:
            gc.SetBrush(wx.Brush(self.GetParent().GetBackgroundColour()))
        else:
            gc.SetBrush(wx.Brush(bg))

        if self._border is not None:
            gc.SetPen(wx.Pen(self._border, 1))
        else:
            gc.SetPen(wx.Pen(bg, 0) if not self._flat else wx.TRANSPARENT_PEN)

        gc.DrawRoundedRectangle(0.5, 0.5, width - 1, height - 1, self._radius)

        gc.SetFont(self.GetFont(), fg)
        text_w, text_h = gc.GetTextExtent(self._label)
        gc.DrawText(self._label, (width - text_w) / 2.0, (height - text_h) / 2.0)


class RoundedPanel(wx.Panel):
    """Panel with a rounded-rectangle custom background (used for cards/bars)."""

    def __init__(self, parent: wx.Window, bg: wx.Colour, border: wx.Colour, radius: int = 14) -> None:
        super().__init__(parent, style=wx.BORDER_NONE)
        self._bg = bg
        self._border = border
        self._radius = radius
        self.SetBackgroundColour(bg)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_size)

    def _on_size(self, event: wx.SizeEvent) -> None:
        # Re-layout children (a bare Refresh handler would suppress the
        # panel's default auto-layout, leaving child controls at 0/default size).
        self.Layout()
        self.Refresh()
        event.Skip()

    def _on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        gc = wx.GraphicsContext.Create(dc)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        if gc is None:
            return
        width, height = self.GetClientSize()
        if width <= 1 or height <= 1:
            return
        gc.SetPen(wx.Pen(self._border, 1))
        gc.SetBrush(wx.Brush(self._bg))
        gc.DrawRoundedRectangle(0.5, 0.5, width - 1, height - 1, self._radius)
