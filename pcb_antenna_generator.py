#!/usr/bin/env python3
"""
Generateur d'antenne PCB reconfigurable (3 frequences) avec interface graphique.

Modele electrique utilise:
- Resonateur quart d'onde imprime (microstrip ouvert)
- Permittivite effective de Hammerstad/Jensen
- Correction de frange (open-end) classique

Sorties:
- Dimensions calculees pour 3 branches commutables
- Geometrie 2D meandree
- Export DXF (R12)
- Rapport texte
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

C0 = 299_792_458.0


@dataclass
class BranchDesign:
    name: str
    frequency_hz: float
    eps_eff: float
    delta_l_mm: float
    quarter_wave_mm: float
    switch_len_mm: float
    radiator_len_mm: float
    total_branch_mm: float


@dataclass
class AntennaDesign:
    branches: list[BranchDesign]
    trunk_height_mm: float
    board_width_mm: float
    board_height_mm: float
    polylines: list[list[tuple[float, float]]]


def effective_permittivity(er: float, h_mm: float, w_mm: float) -> float:
    """Permittivite effective microstrip (Hammerstad/Jensen)."""
    if h_mm <= 0 or w_mm <= 0:
        raise ValueError("h et w doivent etre strictement positifs.")

    u = w_mm / h_mm
    term = 1.0 / math.sqrt(1.0 + 12.0 / u)
    if u <= 1.0:
        term += 0.04 * (1.0 - u) ** 2
    eps_eff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * term
    return eps_eff


def open_end_extension_mm(eps_eff: float, h_mm: float, w_mm: float) -> float:
    """Extension electrique de frange a l'extremite ouverte (Hammerstad)."""
    u = w_mm / h_mm
    delta_l_over_h = 0.412 * ((eps_eff + 0.3) * (u + 0.264)) / ((eps_eff - 0.258) * (u + 0.8))
    return delta_l_over_h * h_mm


def branch_length_for_frequency(f_hz: float, er: float, h_mm: float, w_mm: float, switch_len_mm: float) -> BranchDesign:
    if f_hz <= 0:
        raise ValueError("La frequence doit etre > 0.")
    eps_eff = effective_permittivity(er, h_mm, w_mm)
    delta_l = open_end_extension_mm(eps_eff, h_mm, w_mm)

    quarter_wave_mm = (C0 / (4.0 * f_hz * math.sqrt(eps_eff))) * 1000.0

    # Longueur physique: resonateur quart d'onde moins correction de frange et zone de switch.
    radiator_len_mm = quarter_wave_mm - delta_l - switch_len_mm
    if radiator_len_mm <= 0.5:
        raise ValueError("Longueur calculee non valide. Ajuster les parametres.")

    return BranchDesign(
        name="",
        frequency_hz=f_hz,
        eps_eff=eps_eff,
        delta_l_mm=delta_l,
        quarter_wave_mm=quarter_wave_mm,
        switch_len_mm=switch_len_mm,
        radiator_len_mm=radiator_len_mm,
        total_branch_mm=switch_len_mm + radiator_len_mm,
    )


def meander_path(
    x0: float,
    y0: float,
    length_mm: float,
    segment_mm: float,
    pitch_mm: float,
) -> list[tuple[float, float]]:
    """Genere un chemin meandre pour respecter une longueur donnee."""
    if segment_mm <= 0 or pitch_mm <= 0:
        raise ValueError("segment_mm et pitch_mm doivent etre > 0.")

    pts: list[tuple[float, float]] = [(x0, y0)]
    x, y = x0, y0
    remaining = length_mm
    direction = 1.0

    while remaining > 1e-6:
        dx = min(segment_mm, remaining)
        x += direction * dx
        pts.append((x, y))
        remaining -= dx
        if remaining <= 1e-6:
            break

        dy = min(pitch_mm, remaining)
        y += dy
        pts.append((x, y))
        remaining -= dy
        direction *= -1.0

    return pts


def build_geometry(
    branch_designs: list[BranchDesign],
    switch_len_mm: float,
    branch_spacing_mm: float,
    meander_segment_mm: float,
    meander_pitch_mm: float,
    margin_mm: float,
) -> AntennaDesign:
    # Tronc vertical avec 3 T-junctions, une branche par frequence.
    polylines: list[list[tuple[float, float]]] = []
    branch_count = len(branch_designs)
    trunk_height = (branch_count - 1) * branch_spacing_mm

    # Tronc principal
    trunk = [(0.0, 0.0), (0.0, trunk_height)]
    polylines.append(trunk)

    branch_paths: list[list[tuple[float, float]]] = []

    for i, b in enumerate(branch_designs):
        y = i * branch_spacing_mm

        # Segment switch (zone reservee pour diode PIN/RF switch)
        x_switch_end = switch_len_mm
        branch = [(0.0, y), (x_switch_end, y)]

        # Partie rayonnante meandree
        meander = meander_path(
            x0=x_switch_end,
            y0=y,
            length_mm=b.radiator_len_mm,
            segment_mm=meander_segment_mm,
            pitch_mm=meander_pitch_mm,
        )
        branch.extend(meander[1:])
        branch_paths.append(branch)
        polylines.append(branch)

    # Encombrement pour contour carte recommande.
    all_pts = [pt for poly in polylines for pt in poly]
    min_x = min(p[0] for p in all_pts)
    max_x = max(p[0] for p in all_pts)
    min_y = min(p[1] for p in all_pts)
    max_y = max(p[1] for p in all_pts)

    board_w = (max_x - min_x) + 2.0 * margin_mm
    board_h = (max_y - min_y) + 2.0 * margin_mm

    return AntennaDesign(
        branches=branch_designs,
        trunk_height_mm=trunk_height,
        board_width_mm=board_w,
        board_height_mm=board_h,
        polylines=polylines,
    )


def polyline_length(poly: list[tuple[float, float]]) -> float:
    length = 0.0
    for i in range(1, len(poly)):
        x1, y1 = poly[i - 1]
        x2, y2 = poly[i]
        length += math.hypot(x2 - x1, y2 - y1)
    return length


def export_dxf(path: Path, design: AntennaDesign) -> None:
    """Export DXF R12 minimal (entites LINE en mm)."""
    lines: list[str] = []

    def add(code: str, value: str) -> None:
        lines.append(str(code))
        lines.append(str(value))

    add("0", "SECTION")
    add("2", "HEADER")
    add("9", "$INSUNITS")
    add("70", "4")  # 4 = millimetres
    add("0", "ENDSEC")

    add("0", "SECTION")
    add("2", "ENTITIES")

    for poly in design.polylines:
        for i in range(1, len(poly)):
            x1, y1 = poly[i - 1]
            x2, y2 = poly[i]
            add("0", "LINE")
            add("8", "ANTENNA")
            add("10", f"{x1:.6f}")
            add("20", f"{y1:.6f}")
            add("30", "0.0")
            add("11", f"{x2:.6f}")
            add("21", f"{y2:.6f}")
            add("31", "0.0")

    add("0", "ENDSEC")
    add("0", "EOF")

    path.write_text("\n".join(lines), encoding="ascii")


def design_report(design: AntennaDesign, trace_width_mm: float, er: float, h_mm: float) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out: list[str] = []
    out.append("=== GENERATEUR ANTENNE PCB RECONFIGURABLE (3 BANDES) ===")
    out.append(f"Date: {now}")
    out.append("")
    out.append("Modeles et equations utilises:")
    out.append("1) eps_eff (Hammerstad/Jensen):")
    out.append("   eps_eff = (er+1)/2 + (er-1)/2 * [1/sqrt(1+12h/w) + 0.04(1-w/h)^2 pour w/h<=1]")
    out.append("2) Extension de frange ouverte:")
    out.append("   DeltaL/h = 0.412 * ((eps_eff+0.3)(w/h+0.264))/((eps_eff-0.258)(w/h+0.8))")
    out.append("3) Resonance quart d'onde:")
    out.append("   L_qw = c / (4 f sqrt(eps_eff))")
    out.append("4) Longueur physique branche:")
    out.append("   L_phys = L_qw - DeltaL - L_switch")
    out.append("")
    out.append("Parametres substrate/ligne:")
    out.append(f"- er = {er:.4f}")
    out.append(f"- h = {h_mm:.4f} mm")
    out.append(f"- largeur piste w = {trace_width_mm:.4f} mm")
    out.append("")

    for idx, b in enumerate(design.branches, start=1):
        out.append(f"Branche {idx} ({b.name}):")
        out.append(f"- frequence cible = {b.frequency_hz / 1e6:.3f} MHz")
        out.append(f"- eps_eff = {b.eps_eff:.5f}")
        out.append(f"- DeltaL = {b.delta_l_mm:.4f} mm")
        out.append(f"- L quart d'onde = {b.quarter_wave_mm:.4f} mm")
        out.append(f"- L switch reservee = {b.switch_len_mm:.4f} mm")
        out.append(f"- L rayonnante = {b.radiator_len_mm:.4f} mm")
        out.append(f"- L totale branche = {b.total_branch_mm:.4f} mm")
        out.append("")

    out.append("Implantation recommandee:")
    out.append("- Tronc vertical commun + 3 branches meandrees commutables (diodes PIN ou switch RF).")
    out.append("- Une seule branche active a la fois pour selectionner la frequence.")
    out.append(f"- Encombrement mini suggere: {design.board_width_mm:.2f} mm x {design.board_height_mm:.2f} mm")
    out.append("")
    out.append("Validation conseillee:")
    out.append("- Simuler S11 et rendement (CST/HFSS/FEKO/openEMS).")
    out.append("- Ajuster longueur de chaque branche (+/-2 a 5%) selon boitier, plan de masse, proximite composants.")
    out.append("- Verifier reseau de polarisation des commutateurs pour minimiser pertes RF.")
    return "\n".join(out)


class AntennaApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Generateur d'antenne PCB reconfigurable (3 frequences)")
        self.design: AntennaDesign | None = None

        self.vars: dict[str, tk.StringVar] = {
            "f1_mhz": tk.StringVar(value="433"),
            "f2_mhz": tk.StringVar(value="868"),
            "f3_mhz": tk.StringVar(value="2400"),
            "er": tk.StringVar(value="4.3"),
            "h_mm": tk.StringVar(value="1.6"),
            "w_mm": tk.StringVar(value="2.0"),
            "switch_len_mm": tk.StringVar(value="1.5"),
            "branch_spacing_mm": tk.StringVar(value="8.0"),
            "meander_segment_mm": tk.StringVar(value="14.0"),
            "meander_pitch_mm": tk.StringVar(value="2.5"),
            "margin_mm": tk.StringVar(value="6.0"),
        }

        self._build_ui()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        inputs = ttk.LabelFrame(frame, text="Parametres", padding=8)
        inputs.grid(row=0, column=0, sticky="nsew")

        labels = [
            ("Frequence 1 (MHz)", "f1_mhz"),
            ("Frequence 2 (MHz)", "f2_mhz"),
            ("Frequence 3 (MHz)", "f3_mhz"),
            ("Permittivite relative er", "er"),
            ("Epaisseur substrate h (mm)", "h_mm"),
            ("Largeur piste w (mm)", "w_mm"),
            ("Longueur zone switch (mm)", "switch_len_mm"),
            ("Espacement entre branches (mm)", "branch_spacing_mm"),
            ("Segment meandre horizontal (mm)", "meander_segment_mm"),
            ("Pas meandre vertical (mm)", "meander_pitch_mm"),
            ("Marge contour carte (mm)", "margin_mm"),
        ]

        for r, (text, key) in enumerate(labels):
            ttk.Label(inputs, text=text).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(inputs, textvariable=self.vars[key], width=12).grid(row=r, column=1, sticky="ew", pady=2)

        inputs.columnconfigure(1, weight=1)

        btns = ttk.Frame(frame)
        btns.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="Calculer", command=self.compute).grid(row=0, column=0, padx=2)
        ttk.Button(btns, text="Exporter DXF", command=self.save_dxf).grid(row=0, column=1, padx=2)
        ttk.Button(btns, text="Sauver rapport", command=self.save_report).grid(row=0, column=2, padx=2)

        eq = ttk.LabelFrame(frame, text="Equations utilisees", padding=8)
        eq.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        eq_text = (
            "eps_eff = (er+1)/2 + (er-1)/2 * [1/sqrt(1+12h/w) + correction w/h<=1]\n"
            "DeltaL/h = 0.412 * ((eps_eff+0.3)(w/h+0.264))/((eps_eff-0.258)(w/h+0.8))\n"
            "L_qw = c / (4 f sqrt(eps_eff))\n"
            "L_branche = L_qw - DeltaL - L_switch"
        )
        ttk.Label(eq, text=eq_text, justify="left").grid(row=0, column=0, sticky="w")

        result_frame = ttk.LabelFrame(frame, text="Resultats", padding=8)
        result_frame.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        frame.rowconfigure(3, weight=1)
        frame.columnconfigure(0, weight=1)

        self.result_text = ScrolledText(result_frame, width=90, height=24)
        self.result_text.grid(row=0, column=0, sticky="nsew")
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)

    def _get_float(self, key: str) -> float:
        try:
            return float(self.vars[key].get().strip())
        except Exception as exc:
            raise ValueError(f"Valeur invalide pour {key}") from exc

    def compute(self) -> None:
        try:
            f_values = [
                self._get_float("f1_mhz") * 1e6,
                self._get_float("f2_mhz") * 1e6,
                self._get_float("f3_mhz") * 1e6,
            ]
            if len(set(round(f, 3) for f in f_values)) < 3:
                raise ValueError("Les 3 frequences doivent etre differentes.")

            er = self._get_float("er")
            h_mm = self._get_float("h_mm")
            w_mm = self._get_float("w_mm")
            switch_len_mm = self._get_float("switch_len_mm")
            branch_spacing_mm = self._get_float("branch_spacing_mm")
            meander_segment_mm = self._get_float("meander_segment_mm")
            meander_pitch_mm = self._get_float("meander_pitch_mm")
            margin_mm = self._get_float("margin_mm")

            if er <= 1.0:
                raise ValueError("er doit etre > 1.")

            # Nommer les branches selon ordre de frequence (basse, moyenne, haute)
            sorted_freqs = sorted(f_values)
            names = ["Bande basse", "Bande moyenne", "Bande haute"]

            branches: list[BranchDesign] = []
            for n, f in zip(names, sorted_freqs):
                b = branch_length_for_frequency(f, er, h_mm, w_mm, switch_len_mm)
                b.name = n
                branches.append(b)

            design = build_geometry(
                branch_designs=branches,
                switch_len_mm=switch_len_mm,
                branch_spacing_mm=branch_spacing_mm,
                meander_segment_mm=meander_segment_mm,
                meander_pitch_mm=meander_pitch_mm,
                margin_mm=margin_mm,
            )
            self.design = design

            rep = design_report(design, trace_width_mm=w_mm, er=er, h_mm=h_mm)

            # Ajouter la verification longueur geometrique de chaque branche.
            rep += "\n\nVerification geometrique:\n"
            for idx, poly in enumerate(design.polylines[1:], start=1):
                lgeom = polyline_length(poly)
                target = design.branches[idx - 1].total_branch_mm
                rep += f"- Branche {idx}: longueur tracee = {lgeom:.4f} mm (cible {target:.4f} mm)\n"

            rep += "\nTable de commutation suggeree:\n"
            rep += "- Etat 1: switch bande basse ON, autres OFF\n"
            rep += "- Etat 2: switch bande moyenne ON, autres OFF\n"
            rep += "- Etat 3: switch bande haute ON, autres OFF\n"

            self.result_text.delete("1.0", tk.END)
            self.result_text.insert("1.0", rep)

        except Exception as exc:
            messagebox.showerror("Erreur", str(exc))

    def save_dxf(self) -> None:
        if self.design is None:
            messagebox.showwarning("Info", "Calcule d'abord une antenne.")
            return

        file_path = filedialog.asksaveasfilename(
            title="Exporter DXF",
            defaultextension=".dxf",
            filetypes=[("DXF", "*.dxf")],
            initialfile="antenne_reconfigurable_3bandes.dxf",
        )
        if not file_path:
            return

        try:
            export_dxf(Path(file_path), self.design)
            messagebox.showinfo("OK", f"DXF exporte: {file_path}")
        except Exception as exc:
            messagebox.showerror("Erreur", str(exc))

    def save_report(self) -> None:
        txt = self.result_text.get("1.0", tk.END).strip()
        if not txt:
            messagebox.showwarning("Info", "Aucun rapport a sauver.")
            return

        file_path = filedialog.asksaveasfilename(
            title="Sauver rapport",
            defaultextension=".txt",
            filetypes=[("Texte", "*.txt")],
            initialfile="rapport_antenne_reconfigurable.txt",
        )
        if not file_path:
            return

        try:
            Path(file_path).write_text(txt + "\n", encoding="utf-8")
            messagebox.showinfo("OK", f"Rapport sauve: {file_path}")
        except Exception as exc:
            messagebox.showerror("Erreur", str(exc))


def main() -> None:
    root = tk.Tk()
    app = AntennaApp(root)
    app.compute()
    root.mainloop()


if __name__ == "__main__":
    main()
