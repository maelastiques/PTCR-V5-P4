from typing import Any, Dict, List

import pcbnew


class ReviewWindow:
    @staticmethod
    def _build_text(report: Dict[str, Any], files: List[str]) -> str:
        lines = ["AI Review completed", ""]
        lines.append(report.get("summary", "No summary"))
        lines.append("")
        lines.append("Findings:")
        for item in report.get("findings", []):
            level = item.get("level", "info").upper()
            title = item.get("title", "")
            details = item.get("details", "")
            lines.append(f"- [{level}] {title}: {details}")
        lines.append("")
        lines.append("Generated files:")
        for path in files:
            lines.append(f"- {path}")
        return "\n".join(lines)

    @staticmethod
    def show_report(report: Dict[str, Any], files: List[str]) -> None:
        text = ReviewWindow._build_text(report, files)
        # KiCad Python API differs across versions/builds, so keep UI fallbacks.
        if hasattr(pcbnew, "wxMessageBox"):
            pcbnew.wxMessageBox(text, "Maelectrix")
            return

        try:
            import wx

            wx.MessageBox(text, "Maelectrix", wx.OK | wx.ICON_INFORMATION)
            return
        except Exception:
            pass

        print(text)
