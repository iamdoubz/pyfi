"""
main.py — WiFi Heatmap Generator GUI
Cross-platform tkinter application for collecting and visualizing WiFi signal data.

Usage:
    python main.py                    # Launch GUI
    python main.py --session file.json  # Load existing session

Requirements:
    pip install numpy scipy matplotlib Pillow
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import time
import os
import sys
import argparse
from typing import Optional

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from scanner import scan_averaged, AccessPoint
from data import Session, Measurement
from renderer import render_heatmap, SIGNAL_CMAP


# ── Color palette ─────────────────────────────────────────────────────────────
BG       = "#0d0d1a"
BG2      = "#131325"
BG3      = "#1a1a35"
ACCENT   = "#3d7fff"
ACCENT2  = "#00d4aa"
TEXT     = "#e8e8f0"
TEXT_DIM = "#6a6a8a"
DANGER   = "#ff4a6e"
SUCCESS  = "#00d4aa"
WARNING  = "#ffaa00"
BORDER   = "#2a2a4a"


class WiFiHeatmapApp:
    def __init__(self, root: tk.Tk, initial_session: Optional[str] = None):
        self.root = root
        self.root.title("WiFi Heatmap Generator")
        self.root.configure(bg=BG)
        self.root.minsize(1100, 700)

        self.session: Session = Session()
        self.session_path: Optional[str] = None
        self.selected_bssid: Optional[str] = None
        self.scanning = False
        self.last_scan: list[AccessPoint] = []

        self._build_ui()
        self._apply_styles()

        if initial_session and os.path.exists(initial_session):
            self._load_session(initial_session)
        else:
            self._new_session()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        """Build the main layout: sidebar left, canvas center, panel right."""
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Left sidebar
        self.sidebar = tk.Frame(self.root, bg=BG2, width=220)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 1))
        self.sidebar.grid_propagate(False)
        self._build_sidebar()

        # Center canvas
        self.center = tk.Frame(self.root, bg=BG)
        self.center.grid(row=0, column=1, sticky="nsew")
        self.center.columnconfigure(0, weight=1)
        self.center.rowconfigure(1, weight=1)
        self._build_canvas_area()

        # Right panel
        self.right = tk.Frame(self.root, bg=BG2, width=260)
        self.right.grid(row=0, column=2, sticky="nsew", padx=(1, 0))
        self.right.grid_propagate(False)
        self._build_right_panel()

    def _build_sidebar(self):
        f = self.sidebar

        # Logo / title
        tk.Label(f, text="📡", font=("Segoe UI Emoji", 28), bg=BG2, fg=ACCENT).pack(pady=(20, 4))
        tk.Label(f, text="WiFi Heatmap", font=("Georgia", 13, "bold"),
                 bg=BG2, fg=TEXT).pack()
        tk.Label(f, text="Generator", font=("Georgia", 11),
                 bg=BG2, fg=TEXT_DIM).pack(pady=(0, 16))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=4)

        # Session actions
        self._sidebar_btn(f, "✦  New Session", self._new_session)
        self._sidebar_btn(f, "📂  Open Session", self._open_session)
        self._sidebar_btn(f, "💾  Save Session", self._save_session)
        self._sidebar_btn(f, "💾  Save As…", self._save_session_as)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # Floorplan
        tk.Label(f, text="FLOORPLAN", font=("Courier", 8, "bold"),
                 bg=BG2, fg=TEXT_DIM).pack(anchor="w", padx=14)
        self.floorplan_label = tk.Label(
            f, text="None  (grid mode)",
            font=("Courier", 8), bg=BG2, fg=WARNING,
            wraplength=190, justify="left"
        )
        self.floorplan_label.pack(anchor="w", padx=14, pady=(2, 4))
        self.floorplan_note = tk.Label(
            f, text="⚠ Adding a floorplan\nincreases accuracy.",
            font=("Courier", 8), bg=BG2, fg=WARNING,
            justify="left"
        )
        self.floorplan_note.pack(anchor="w", padx=14, pady=(0, 2))
        self._sidebar_btn(f, "🖼  Load Floorplan", self._load_floorplan)
        self._sidebar_btn(f, "✕  Remove Floorplan", self._remove_floorplan)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # Export
        tk.Label(f, text="EXPORT", font=("Courier", 8, "bold"),
                 bg=BG2, fg=TEXT_DIM).pack(anchor="w", padx=14)
        self._sidebar_btn(f, "🖼  Export PNG", self._export_png)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # Session info
        self.session_info = tk.Label(
            f, text="", font=("Courier", 8),
            bg=BG2, fg=TEXT_DIM, wraplength=195, justify="left"
        )
        self.session_info.pack(anchor="w", padx=14, pady=4)

    def _build_canvas_area(self):
        f = self.center

        # Toolbar
        toolbar = tk.Frame(f, bg=BG3, height=44)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(99, weight=1)

        tk.Label(toolbar, text="  Click on the canvas to record a measurement point",
                 font=("Courier", 9), bg=BG3, fg=TEXT_DIM).grid(row=0, column=0, padx=8, sticky="w")

        self.scan_btn = tk.Button(
            toolbar, text="▶  Scan & Place",
            font=("Courier", 9, "bold"), bg=ACCENT, fg="white",
            relief="flat", padx=12, pady=6, cursor="hand2",
            activebackground="#5590ff", activeforeground="white",
            command=self._begin_placement_mode
        )
        self.scan_btn.grid(row=0, column=1, padx=6, pady=6)

        self.undo_btn = tk.Button(
            toolbar, text="↩  Undo",
            font=("Courier", 9), bg=BG2, fg=TEXT,
            relief="flat", padx=10, pady=6, cursor="hand2",
            activebackground=BG3, command=self._undo_last
        )
        self.undo_btn.grid(row=0, column=2, padx=4, pady=6)

        self.status_label = tk.Label(
            toolbar, text="Ready", font=("Courier", 9),
            bg=BG3, fg=TEXT_DIM
        )
        self.status_label.grid(row=0, column=99, padx=12, sticky="e")

        # Matplotlib canvas
        self.fig = Figure(figsize=(9, 6), dpi=100)
        self.fig.patch.set_facecolor(BG)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor(BG)

        self.canvas_widget = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas_widget.get_tk_widget().grid(row=1, column=0, sticky="nsew")
        self.canvas_widget.mpl_connect("button_press_event", self._on_canvas_click)

        # Placement mode overlay state
        self._placement_mode = False
        self._pending_scan: Optional[list[AccessPoint]] = None

    def _build_right_panel(self):
        f = self.right

        # Access Points list
        tk.Label(f, text="ACCESS POINTS", font=("Courier", 9, "bold"),
                 bg=BG2, fg=TEXT_DIM).pack(anchor="w", padx=14, pady=(16, 4))

        self.ap_scan_btn = tk.Button(
            f, text="🔄  Refresh Scan",
            font=("Courier", 8), bg=BG3, fg=ACCENT2,
            relief="flat", padx=8, pady=4, cursor="hand2",
            activebackground=BG, command=self._refresh_scan
        )
        self.ap_scan_btn.pack(anchor="w", padx=12, pady=(0, 6))

        # Listbox for APs
        list_frame = tk.Frame(f, bg=BG2)
        list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        self.ap_listbox = tk.Listbox(
            list_frame,
            font=("Courier", 8),
            bg=BG3, fg=TEXT, selectbackground=ACCENT,
            selectforeground="white", relief="flat",
            borderwidth=0, highlightthickness=1,
            highlightcolor=BORDER, highlightbackground=BORDER,
            activestyle="none",
            yscrollcommand=scrollbar.set
        )
        self.ap_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.ap_listbox.yview)
        self.ap_listbox.bind("<<ListboxSelect>>", self._on_ap_select)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=8, pady=4)

        # Selected AP detail
        tk.Label(f, text="SELECTED AP", font=("Courier", 9, "bold"),
                 bg=BG2, fg=TEXT_DIM).pack(anchor="w", padx=14, pady=(4, 2))
        self.ap_detail = tk.Label(
            f, text="None selected",
            font=("Courier", 8), bg=BG2, fg=TEXT,
            wraplength=235, justify="left"
        )
        self.ap_detail.pack(anchor="w", padx=14)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=8, pady=8)

        # Measurements list
        tk.Label(f, text="MEASUREMENTS", font=("Courier", 9, "bold"),
                 bg=BG2, fg=TEXT_DIM).pack(anchor="w", padx=14, pady=(0, 4))

        self.meas_count_label = tk.Label(
            f, text="0 points collected",
            font=("Courier", 8), bg=BG2, fg=ACCENT2
        )
        self.meas_count_label.pack(anchor="w", padx=14, pady=(0, 4))

        meas_frame = tk.Frame(f, bg=BG2)
        meas_frame.pack(fill="x", padx=8, pady=(0, 8))

        meas_scroll = ttk.Scrollbar(meas_frame)
        meas_scroll.pack(side="right", fill="y")

        self.meas_listbox = tk.Listbox(
            meas_frame,
            font=("Courier", 7),
            bg=BG3, fg=TEXT_DIM, selectbackground=DANGER,
            relief="flat", borderwidth=0,
            highlightthickness=1, highlightcolor=BORDER, highlightbackground=BORDER,
            activestyle="none", height=8,
            yscrollcommand=meas_scroll.set
        )
        self.meas_listbox.pack(side="left", fill="x", expand=True)
        meas_scroll.config(command=self.meas_listbox.yview)

        self.del_meas_btn = tk.Button(
            f, text="🗑  Delete Selected",
            font=("Courier", 8), bg=BG3, fg=DANGER,
            relief="flat", padx=8, pady=4, cursor="hand2",
            command=self._delete_measurement
        )
        self.del_meas_btn.pack(anchor="w", padx=12, pady=(0, 12))

    def _sidebar_btn(self, parent, text, command):
        btn = tk.Button(
            parent, text=text,
            font=("Courier", 9), bg=BG2, fg=TEXT,
            relief="flat", anchor="w", padx=14, pady=5,
            cursor="hand2", activebackground=BG3, activeforeground=TEXT,
            command=command
        )
        btn.pack(fill="x", padx=4, pady=1)
        return btn

    def _apply_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TSeparator", background=BORDER)
        style.configure("TScrollbar",
                         background=BG3, troughcolor=BG2,
                         bordercolor=BORDER, arrowcolor=TEXT_DIM)

    # ── Session management ─────────────────────────────────────────────────────

    def _new_session(self):
        if self.session.measurements:
            if not messagebox.askyesno("New Session", "Discard current session?"):
                return
        self.session = Session(name="New Session")
        self.session_path = None
        self.selected_bssid = None
        self._refresh_everything()
        self._set_status("New session started")

    def _open_session(self):
        path = filedialog.askopenfilename(
            title="Open Session",
            filetypes=[("Heatmap Session", "*.json"), ("All Files", "*.*")]
        )
        if path:
            self._load_session(path)

    def _load_session(self, path: str):
        try:
            self.session = Session.load(path)
            self.session_path = path
            self._refresh_everything()
            self._set_status(f"Loaded: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Load Error", str(e))

    def _save_session(self):
        if not self.session_path:
            self._save_session_as()
            return
        try:
            self.session.save(self.session_path)
            self._set_status(f"Saved: {os.path.basename(self.session_path)}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _save_session_as(self):
        path = filedialog.asksaveasfilename(
            title="Save Session As",
            defaultextension=".json",
            filetypes=[("Heatmap Session", "*.json")]
        )
        if path:
            self.session_path = path
            self._save_session()

    def _load_floorplan(self):
        path = filedialog.askopenfilename(
            title="Load Floorplan Image",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.tiff"),
                ("All Files", "*.*")
            ]
        )
        if path:
            self.session.floorplan_path = path
            self._update_floorplan_label()
            self._redraw()
            self._set_status("Floorplan loaded")

    def _remove_floorplan(self):
        self.session.floorplan_path = None
        self._update_floorplan_label()
        self._redraw()
        self._set_status("Floorplan removed — using grid mode")

    def _update_floorplan_label(self):
        if self.session.floorplan_path:
            name = os.path.basename(self.session.floorplan_path)
            self.floorplan_label.config(text=f"✓ {name}", fg=SUCCESS)
            self.floorplan_note.config(text="")
        else:
            self.floorplan_label.config(text="None  (grid mode)", fg=WARNING)
            self.floorplan_note.config(text="⚠ Adding a floorplan\nincreases accuracy.")

    # ── Scanning & placement ───────────────────────────────────────────────────

    def _refresh_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.ap_scan_btn.config(text="⏳  Scanning…", state="disabled")
        self._set_status("Scanning for access points…")
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        try:
            aps = scan_averaged(samples=3)
            self.root.after(0, lambda: self._scan_complete(aps))
        except Exception as e:
            self.root.after(0, lambda: self._scan_error(str(e)))

    def _scan_complete(self, aps: list[AccessPoint]):
        self.scanning = False
        self.last_scan = aps
        self.ap_scan_btn.config(text="🔄  Refresh Scan", state="normal")
        self._populate_ap_list(aps)
        self._set_status(f"Found {len(aps)} access point{'s' if len(aps) != 1 else ''}")

    def _scan_error(self, err: str):
        self.scanning = False
        self.ap_scan_btn.config(text="🔄  Refresh Scan", state="normal")
        self._set_status(f"Scan failed: {err}", error=True)
        messagebox.showerror("Scan Error",
            f"WiFi scan failed:\n{err}\n\n"
            "On Linux, try: sudo nmcli dev wifi list --rescan yes\n"
            "On Windows, run as Administrator if needed.")

    def _populate_ap_list(self, aps: list[AccessPoint]):
        self.ap_listbox.delete(0, tk.END)
        for ap in aps:
            bar = _signal_bar(ap.signal_dbm)
            entry = f"{bar}  {ap.ssid[:16]:<16}  {ap.signal_dbm:>4}dBm"
            self.ap_listbox.insert(tk.END, entry)

        # Also refresh session APs not in current scan
        seen = {ap.bssid for ap in aps}
        for bssid in self.session.get_all_bssids():
            if bssid not in seen:
                ssid = self.session.get_ssid(bssid)
                self.ap_listbox.insert(tk.END, f"[stored]  {ssid[:16]:<16}  {bssid}")

    def _on_ap_select(self, event):
        sel = self.ap_listbox.curselection()
        if not sel:
            return
        idx = sel[0]

        if idx < len(self.last_scan):
            ap = self.last_scan[idx]
            self.selected_bssid = ap.bssid
            self.ap_detail.config(
                text=f"SSID:  {ap.ssid}\n"
                     f"BSSID: {ap.bssid}\n"
                     f"Signal: {ap.signal_dbm} dBm ({ap.signal_percent()}%)\n"
                     f"Quality: {ap.quality_label()}\n"
                     f"Channel: {ap.channel}  {ap.band}"
            )
        else:
            # Session-only AP
            bssid_offset = idx - len(self.last_scan)
            bssids = [b for b in self.session.get_all_bssids()
                      if b not in {a.bssid for a in self.last_scan}]
            if bssid_offset < len(bssids):
                self.selected_bssid = bssids[bssid_offset]
                ssid = self.session.get_ssid(self.selected_bssid)
                self.ap_detail.config(
                    text=f"SSID:  {ssid}\nBSSID: {self.selected_bssid}\n(from saved data)"
                )

        self._redraw()

    def _begin_placement_mode(self):
        """Scan and then switch to placement mode — next canvas click records a point."""
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.config(text="⏳ Scanning…", state="disabled")
        self._set_status("Scanning… Click your location on the map when ready.")
        threading.Thread(target=self._scan_for_placement, daemon=True).start()

    def _scan_for_placement(self):
        try:
            aps = scan_averaged(samples=5)
            self.root.after(0, lambda: self._ready_to_place(aps))
        except Exception as e:
            self.root.after(0, lambda: self._scan_error(str(e)))

    def _ready_to_place(self, aps: list[AccessPoint]):
        self.scanning = False
        self._pending_scan = aps
        self._placement_mode = True
        self._populate_ap_list(aps)
        self.scan_btn.config(text="📍 Click your location", state="normal", bg=ACCENT2)
        self._set_status("✓ Scan complete — now click your position on the map")
        self.canvas_widget.get_tk_widget().config(cursor="crosshair")

    def _on_canvas_click(self, event):
        if event.inaxes != self.ax:
            return
        if not self._placement_mode or not self._pending_scan:
            return

        x, y = event.xdata, event.ydata
        if x is None or y is None:
            return

        # Build signals dict and ssid_map from scan results
        signals = {ap.bssid: ap.signal_dbm for ap in self._pending_scan}
        ssid_map = {ap.bssid: ap.ssid for ap in self._pending_scan}

        m = Measurement(x=x, y=y, signals=signals, ssid_map=ssid_map)
        self.session.add_measurement(m)

        # Exit placement mode
        self._placement_mode = False
        self._pending_scan = None
        self.scan_btn.config(text="▶  Scan & Place", bg=ACCENT)
        self.canvas_widget.get_tk_widget().config(cursor="")

        point_count = len(self.session.measurements)
        self._set_status(f"✓ Point #{point_count} recorded at ({int(x)}, {int(y)})")
        self._refresh_everything()

    def _undo_last(self):
        if not self.session.measurements:
            return
        self.session.remove_measurement(len(self.session.measurements) - 1)
        self._set_status("Last measurement removed")
        self._refresh_everything()

    def _delete_measurement(self):
        sel = self.meas_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.session.remove_measurement(idx)
        self._refresh_everything()
        self._set_status(f"Measurement {idx + 1} deleted")

    # ── Rendering & export ─────────────────────────────────────────────────────

    def _redraw(self):
        if self.selected_bssid:
            try:
                render_heatmap(self.session, self.selected_bssid, self.fig, self.ax)
            except Exception as e:
                self.ax.clear()
                self.ax.text(0.5, 0.5, f"Render error:\n{e}",
                             transform=self.ax.transAxes, ha='center', va='center',
                             color='red', fontsize=10)
        else:
            self._draw_empty_canvas()
        self.canvas_widget.draw()

    def _draw_empty_canvas(self):
        self.ax.clear()
        self.ax.set_facecolor(BG)
        self.fig.patch.set_facecolor(BG)
        w = self.session.canvas_width
        h = self.session.canvas_height

        if self.session.floorplan_path:
            from PIL import Image
            import numpy as np
            try:
                img = Image.open(self.session.floorplan_path).convert("RGBA")
                img = img.resize((w, h), Image.LANCZOS)
                self.ax.imshow(np.array(img), extent=[0, w, h, 0], aspect='auto', alpha=0.4)
            except Exception:
                pass
        else:
            # Grid background
            spacing = max(w, h) // 10
            for x in range(0, w + 1, spacing):
                self.ax.axvline(x, color='#1e2a4a', linewidth=0.5)
            for y in range(0, h + 1, spacing):
                self.ax.axhline(y, color='#1e2a4a', linewidth=0.5)

        msg = "Select an access point →\nthen click  ▶ Scan & Place  to begin"
        if not self.session.floorplan_path:
            msg += "\n\n⚠ No floorplan loaded. Load one from\nthe sidebar to increase accuracy."

        self.ax.text(w / 2, h / 2, msg,
                     ha='center', va='center', color='#4a4a7a',
                     fontsize=12, linespacing=1.8,
                     bbox=dict(boxstyle='round,pad=0.8', facecolor='#10101e', alpha=0.7))

        self.ax.set_xlim(0, w)
        self.ax.set_ylim(h, 0)
        self.ax.set_xticks([])
        self.ax.set_yticks([])

    def _export_png(self):
        if not self.selected_bssid:
            messagebox.showinfo("Export", "Please select an access point first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export Heatmap",
            defaultextension=".png",
            filetypes=[("PNG Image", "*.png"), ("JPEG Image", "*.jpg")]
        )
        if path:
            try:
                render_heatmap(self.session, self.selected_bssid,
                               export_path=path)
                self._set_status(f"Exported: {os.path.basename(path)}")
                messagebox.showinfo("Export", f"Saved to:\n{path}")
            except Exception as e:
                messagebox.showerror("Export Error", str(e))

    # ── Refresh helpers ────────────────────────────────────────────────────────

    def _refresh_everything(self):
        self._update_measurements_list()
        self._update_session_info()
        self._update_floorplan_label()
        self._refresh_ap_list_from_session()
        self._redraw()

    def _refresh_ap_list_from_session(self):
        """Populate AP list from session data (without re-scanning)."""
        all_bssids = self.session.get_all_bssids()
        if not all_bssids and not self.last_scan:
            return
        if self.last_scan:
            self._populate_ap_list(self.last_scan)
        else:
            self.ap_listbox.delete(0, tk.END)
            for bssid in all_bssids:
                ssid = self.session.get_ssid(bssid)
                pts, vals = self.session.get_points_and_values(bssid)
                avg = int(sum(vals) / len(vals)) if vals else 0
                bar = _signal_bar(avg)
                self.ap_listbox.insert(
                    tk.END, f"{bar}  {ssid[:16]:<16}  {avg:>4}dBm"
                )

    def _update_measurements_list(self):
        self.meas_listbox.delete(0, tk.END)
        for i, m in enumerate(self.session.measurements):
            ap_count = len(m.signals)
            self.meas_listbox.insert(
                tk.END, f"#{i+1:>3}  ({int(m.x):>4}, {int(m.y):>4})  {ap_count} APs"
            )
        count = len(self.session.measurements)
        self.meas_count_label.config(
            text=f"{count} point{'s' if count != 1 else ''} collected"
        )

    def _update_session_info(self):
        name = self.session.name
        path = os.path.basename(self.session_path) if self.session_path else "unsaved"
        fp = "✓ floorplan" if self.session.floorplan_path else "grid mode"
        self.session_info.config(
            text=f"Session: {name}\nFile: {path}\nMode: {fp}"
        )

    def _set_status(self, msg: str, error: bool = False):
        color = DANGER if error else TEXT_DIM
        self.status_label.config(text=msg, fg=color)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _signal_bar(dbm: int) -> str:
    """Return a mini ASCII signal bar for the AP list."""
    pct = max(0, min(100, (dbm + 90) * 100 // 60))
    bars = pct // 20
    return "▂▄▆█"[:bars].ljust(4, "░")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WiFi Heatmap Generator")
    parser.add_argument("--session", help="Path to a .json session file to load on startup")
    args = parser.parse_args()

    root = tk.Tk()
    root.title("WiFi Heatmap Generator")

    # Try to set icon (optional, may fail on some platforms)
    try:
        root.iconbitmap(default="")
    except Exception:
        pass

    app = WiFiHeatmapApp(root, initial_session=args.session)
    root.mainloop()


if __name__ == "__main__":
    main()
