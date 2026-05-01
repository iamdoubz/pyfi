"""
main.py — WiFi Heatmap Generator GUI
Cross-platform tkinter application for collecting and visualizing WiFi signal data.

Usage:
    python main.py                       # Launch GUI
    python main.py --session file.json   # Load existing session

Requirements:
    pip install numpy scipy matplotlib Pillow
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import argparse
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from scanner import scan_averaged, AccessPoint
from data import Session, Measurement
from renderer import render_heatmap, SIGNAL_CMAP


# ── Palette ───────────────────────────────────────────────────────────────────
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

# Signal level colors (match renderer thresholds)
SIG_GREEN  = "#00c853"
SIG_YELLOW = "#ffd600"
SIG_ORANGE = "#ff6d00"
SIG_RED    = "#d50000"


def _sig_color(dbm: int) -> str:
    if dbm >= -50: return SIG_GREEN
    if dbm >= -65: return SIG_YELLOW
    if dbm >= -75: return SIG_ORANGE
    return SIG_RED


@dataclass
class _ApStats:
    """Running statistics accumulated across all auto-scans for one BSSID."""
    last_seen_ts: float = 0.0    # time.monotonic() of last sighting
    max_dbm: int = -999
    min_dbm: int = 0
    total_dbm: int = 0
    count: int = 0

    def update(self, dbm: int):
        self.last_seen_ts = time.monotonic()
        self.max_dbm  = max(self.max_dbm, dbm)
        self.min_dbm  = min(self.min_dbm, dbm) if self.count > 0 else dbm
        self.total_dbm += dbm
        self.count += 1

    @property
    def avg_dbm(self) -> int:
        return self.total_dbm // self.count if self.count else 0

    def last_seen_str(self, now: float) -> str:
        if self.last_seen_ts == 0:
            return "—"
        secs = int(now - self.last_seen_ts)
        if secs < 60:   return f"{secs}s ago"
        if secs < 3600: return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"


# ── AP Table columns ──────────────────────────────────────────────────────────
AP_COLUMNS = [
    ("sel",       "✓",          38),
    ("ssid",      "SSID",      160),
    ("bssid",     "BSSID",     145),
    ("channel",   "Ch",         38),
    ("freq",      "Frequency",  90),
    ("width",     "Ch Width",   75),
    ("band",      "Band",       60),
    ("security",  "Security",  160),
    ("vendor",    "Vendor",     90),
    ("mode",      "Mode",       90),
    ("level",     "Level",     110),
    ("last_seen", "Last Seen",  75),
    ("max_dbm",   "Max",        50),
    ("min_dbm",   "Min",        50),
    ("avg_dbm",   "Avg",        50),
]


class WiFiHeatmapApp:
    def __init__(self, root: tk.Tk, initial_session: Optional[str] = None):
        self.root = root
        self.root.title("WiFi Heatmap Generator")
        self.root.configure(bg=BG)
        self.root.minsize(1200, 720)

        self.session: Session = Session()
        self.session_path: Optional[str] = None
        self.scanning = False
        self.last_scan: list[AccessPoint] = []
        self.selected_bssids: set[str] = set()        # checked on AP tab
        self.selected_bssids_for_heatmap: list[str] = []  # ordered list for rendering

        # Per-BSSID running statistics (accumulated across all auto-scans)
        self._ap_stats: dict[str, _ApStats] = {}

        # Auto-scan state
        self._auto_scan_job: Optional[str] = None   # root.after() handle
        self._auto_scan_enabled: bool = False

        self._build_ui()
        self._apply_styles()

        if initial_session and os.path.exists(initial_session):
            self._load_session(initial_session)
        else:
            self._new_session()

    # ── Top-level layout ──────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Left sidebar (always visible)
        self.sidebar = tk.Frame(self.root, bg=BG2, width=220)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 1))
        self.sidebar.grid_propagate(False)
        self._build_sidebar()

        # Main content area with notebook
        content = tk.Frame(self.root, bg=BG)
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(content)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        # ── Tab 1: Access Points ──────────────────────────────────────────────
        self.ap_tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.ap_tab, text="  Access Points  ")
        self.ap_tab.columnconfigure(0, weight=1)
        self.ap_tab.rowconfigure(1, weight=1)
        self._build_ap_tab()

        # ── Tab 2: Heatmap ────────────────────────────────────────────────────
        self.heatmap_tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.heatmap_tab, text="  Heatmap  ")
        self.heatmap_tab.columnconfigure(0, weight=1)
        self.heatmap_tab.rowconfigure(1, weight=1)
        self._build_heatmap_tab()

        # Start on Access Points tab
        self.notebook.select(self.ap_tab)

    # ── Sidebar ───────────────────────────────────────────────────────────────

    def _build_sidebar(self):
        f = self.sidebar

        tk.Label(f, text="📡", font=("Segoe UI Emoji", 28), bg=BG2, fg=ACCENT).pack(pady=(20, 4))
        tk.Label(f, text="WiFi Heatmap", font=("Georgia", 13, "bold"), bg=BG2, fg=TEXT).pack()
        tk.Label(f, text="Generator",    font=("Georgia", 11),          bg=BG2, fg=TEXT_DIM).pack(pady=(0, 16))

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=4)

        self._sidebar_btn(f, "✦  New Session",    self._new_session)
        self._sidebar_btn(f, "📂  Open Session",   self._open_session)
        self._sidebar_btn(f, "💾  Save Session",   self._save_session)
        self._sidebar_btn(f, "💾  Save As…",       self._save_session_as)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=8)

        tk.Label(f, text="FLOORPLAN", font=("Courier", 8, "bold"), bg=BG2, fg=TEXT_DIM).pack(anchor="w", padx=14)
        self.floorplan_label = tk.Label(f, text="None  (grid mode)",
            font=("Courier", 8), bg=BG2, fg=WARNING, wraplength=190, justify="left")
        self.floorplan_label.pack(anchor="w", padx=14, pady=(2, 2))
        self.floorplan_note = tk.Label(f, text="⚠ Adding a floorplan\nincreases accuracy.",
            font=("Courier", 8), bg=BG2, fg=WARNING, justify="left")
        self.floorplan_note.pack(anchor="w", padx=14, pady=(0, 2))
        self._sidebar_btn(f, "🖼  Load Floorplan",   self._load_floorplan)
        self._sidebar_btn(f, "✕  Remove Floorplan",  self._remove_floorplan)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=8)

        tk.Label(f, text="EXPORT", font=("Courier", 8, "bold"), bg=BG2, fg=TEXT_DIM).pack(anchor="w", padx=14)
        self._sidebar_btn(f, "🖼  Export PNG", self._export_png)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=12, pady=8)

        self.session_info = tk.Label(f, text="", font=("Courier", 8),
            bg=BG2, fg=TEXT_DIM, wraplength=195, justify="left")
        self.session_info.pack(anchor="w", padx=14, pady=4)

    def _sidebar_btn(self, parent, text, command):
        btn = tk.Button(parent, text=text, font=("Courier", 9), bg=BG2, fg=TEXT,
            relief="flat", anchor="w", padx=14, pady=5, cursor="hand2",
            activebackground=BG3, activeforeground=TEXT, command=command)
        btn.pack(fill="x", padx=4, pady=1)
        return btn

    # ── Access Points Tab ─────────────────────────────────────────────────────

    def _build_ap_tab(self):
        f = self.ap_tab

        # Toolbar row 0: title + manual scan + select buttons
        toolbar = tk.Frame(f, bg=BG3)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(10, weight=1)

        tk.Label(toolbar, text="  Discovered Access Points",
                 font=("Courier", 10, "bold"), bg=BG3, fg=TEXT).grid(
                 row=0, column=0, padx=12, pady=(10, 2), sticky="w")

        self.ap_scan_btn = tk.Button(toolbar, text="🔄  Refresh Scan",
            font=("Courier", 9, "bold"), bg=ACCENT, fg="white",
            relief="flat", padx=12, pady=6, cursor="hand2",
            activebackground="#5590ff", activeforeground="white",
            command=self._refresh_scan)
        self.ap_scan_btn.grid(row=0, column=1, padx=8, pady=(8, 2))

        self.ap_select_all_btn = tk.Button(toolbar, text="☑  Select All",
            font=("Courier", 8), bg=BG2, fg=ACCENT2,
            relief="flat", padx=8, pady=6, cursor="hand2",
            command=self._ap_select_all)
        self.ap_select_all_btn.grid(row=0, column=2, padx=4, pady=(8, 2))

        self.ap_deselect_all_btn = tk.Button(toolbar, text="☐  Deselect All",
            font=("Courier", 8), bg=BG2, fg=TEXT_DIM,
            relief="flat", padx=8, pady=6, cursor="hand2",
            command=self._ap_deselect_all)
        self.ap_deselect_all_btn.grid(row=0, column=3, padx=4, pady=(8, 2))

        self.ap_status_label = tk.Label(toolbar, text="No scan yet",
            font=("Courier", 8), bg=BG3, fg=TEXT_DIM)
        self.ap_status_label.grid(row=0, column=10, padx=12, sticky="e")

        # Toolbar row 1: auto-scan controls
        auto_bar = tk.Frame(toolbar, bg=BG3)
        auto_bar.grid(row=1, column=0, columnspan=11, sticky="ew", padx=8, pady=(0, 8))

        tk.Label(auto_bar, text="  Auto-scan:",
                 font=("Courier", 8), bg=BG3, fg=TEXT_DIM).pack(side="left")

        self._auto_scan_var = tk.BooleanVar(value=False)
        self.auto_scan_chk = tk.Checkbutton(
            auto_bar, variable=self._auto_scan_var,
            text="enabled", font=("Courier", 8),
            bg=BG3, fg=ACCENT2, selectcolor=BG2,
            activebackground=BG3, activeforeground=ACCENT2,
            relief="flat", cursor="hand2",
            command=self._toggle_auto_scan)
        self.auto_scan_chk.pack(side="left", padx=(4, 12))

        tk.Label(auto_bar, text="Interval:",
                 font=("Courier", 8), bg=BG3, fg=TEXT_DIM).pack(side="left")

        self._auto_scan_interval = tk.IntVar(value=10)
        interval_spin = tk.Spinbox(
            auto_bar, from_=3, to=300, increment=1,
            textvariable=self._auto_scan_interval,
            font=("Courier", 8), width=4,
            bg=BG2, fg=TEXT, insertbackground=TEXT,
            buttonbackground=BG3, relief="flat",
            command=self._on_interval_change)
        interval_spin.pack(side="left", padx=(4, 2))
        interval_spin.bind("<FocusOut>", self._on_interval_change)
        interval_spin.bind("<Return>",   self._on_interval_change)

        tk.Label(auto_bar, text="seconds",
                 font=("Courier", 8), bg=BG3, fg=TEXT_DIM).pack(side="left", padx=(2, 16))

        self.auto_scan_countdown = tk.Label(auto_bar, text="",
            font=("Courier", 8, "bold"), bg=BG3, fg=WARNING)
        self.auto_scan_countdown.pack(side="left")

        # Canvas for custom AP table (gives us full control over colors/bars)
        table_outer = tk.Frame(f, bg=BG)
        table_outer.grid(row=1, column=0, sticky="nsew")
        table_outer.columnconfigure(0, weight=1)
        table_outer.rowconfigure(1, weight=1)

        # Column headers
        self._build_ap_header(table_outer)

        # Scrollable body
        body_frame = tk.Frame(table_outer, bg=BG)
        body_frame.grid(row=1, column=0, sticky="nsew")
        body_frame.columnconfigure(0, weight=1)
        body_frame.rowconfigure(0, weight=1)

        self.ap_canvas = tk.Canvas(body_frame, bg=BG, highlightthickness=0)
        ap_vscroll = ttk.Scrollbar(body_frame, orient="vertical",   command=self.ap_canvas.yview)
        ap_hscroll = ttk.Scrollbar(body_frame, orient="horizontal", command=self.ap_canvas.xview)

        self.ap_canvas.configure(yscrollcommand=ap_vscroll.set,
                                  xscrollcommand=ap_hscroll.set)
        self.ap_canvas.grid(row=0, column=0, sticky="nsew")
        ap_vscroll.grid(row=0, column=1, sticky="ns")
        ap_hscroll.grid(row=1, column=0, sticky="ew")

        self.ap_rows_frame = tk.Frame(self.ap_canvas, bg=BG)
        self.ap_canvas_window = self.ap_canvas.create_window(
            (0, 0), window=self.ap_rows_frame, anchor="nw")

        self.ap_rows_frame.bind("<Configure>", self._on_ap_frame_configure)
        self.ap_canvas.bind("<Configure>", self._on_ap_canvas_configure)
        self.ap_canvas.bind("<MouseWheel>", lambda e: self.ap_canvas.yview_scroll(-1*(e.delta//120), "units"))
        self.ap_canvas.bind("<Button-4>",   lambda e: self.ap_canvas.yview_scroll(-1, "units"))
        self.ap_canvas.bind("<Button-5>",   lambda e: self.ap_canvas.yview_scroll( 1, "units"))

        # Row data storage
        self._ap_row_widgets: list[dict] = []   # list of {bssid, check_var, row_frame, ...}

    def _build_ap_header(self, parent):
        hdr = tk.Frame(parent, bg=BG3)
        hdr.grid(row=0, column=0, sticky="ew")
        for col_id, label, width in AP_COLUMNS:
            tk.Label(hdr, text=label, font=("Courier", 8, "bold"),
                     bg=BG3, fg=TEXT_DIM, width=max(1, width // 8),
                     anchor="w", padx=6, pady=6).pack(side="left")

    def _on_ap_frame_configure(self, event):
        self.ap_canvas.configure(scrollregion=self.ap_canvas.bbox("all"))

    def _on_ap_canvas_configure(self, event):
        self.ap_canvas.itemconfig(self.ap_canvas_window, width=event.width)

    def _populate_ap_table(self, aps: list[AccessPoint]):
        """Rebuild the AP table rows from a scan result."""
        for w in self.ap_rows_frame.winfo_children():
            w.destroy()
        self._ap_row_widgets.clear()

        now = time.monotonic()

        for i, ap in enumerate(aps):
            row_bg = BG if i % 2 == 0 else BG2
            row    = tk.Frame(self.ap_rows_frame, bg=row_bg)
            row.pack(fill="x", expand=True)

            # Checkbox
            check_var = tk.BooleanVar(value=(ap.bssid in self.selected_bssids))
            tk.Checkbutton(row, variable=check_var, bg=row_bg,
                           activebackground=row_bg, fg=ACCENT2, selectcolor=BG3,
                           relief="flat", cursor="hand2",
                           command=lambda b=ap.bssid, v=check_var: self._on_ap_toggle(b, v)
                           ).pack(side="left", padx=(6, 0))

            def _cell(text, width, fg=TEXT, bold=False):
                tk.Label(row, text=text,
                         font=("Courier", 8, "bold") if bold else ("Courier", 8),
                         bg=row_bg, fg=fg,
                         width=max(1, width // 8), anchor="w",
                         padx=4, pady=5).pack(side="left")

            _cell(ap.ssid[:20],                           160, fg=TEXT,     bold=True)
            _cell(ap.bssid,                               145, fg=TEXT_DIM)
            _cell(str(ap.channel) if ap.channel else "-",  38, fg=TEXT)
            _cell(ap.freq_label(),                         90, fg=TEXT)
            _cell(ap.channel_width or "-",                 75, fg=TEXT)
            _cell(ap.band or "-",                          60, fg=ACCENT2)
            _cell(ap.security or "-",                     160, fg=TEXT)
            _cell(ap.vendor or "-",                        90, fg=TEXT_DIM)
            _cell(ap.mode_label(),                         90, fg=TEXT)

            # Signal level bar
            bar_frame = tk.Frame(row, bg=row_bg, width=110, height=26)
            bar_frame.pack(side="left", padx=4)
            bar_frame.pack_propagate(False)
            self._draw_signal_bar(bar_frame, ap.signal_dbm, row_bg)

            # ── Stat columns ──────────────────────────────────────────────────
            stats = self._ap_stats.get(ap.bssid)
            if stats and stats.count > 0:
                last_seen_str = stats.last_seen_str(now)
                max_str  = f"{stats.max_dbm}"
                min_str  = f"{stats.min_dbm}"
                avg_str  = f"{stats.avg_dbm}"
                stat_fg  = TEXT_DIM
            else:
                last_seen_str = "—"
                max_str = min_str = avg_str = "—"
                stat_fg = TEXT_DIM

            _cell(last_seen_str, 75, fg=stat_fg)
            _cell(max_str,       50, fg=SIG_GREEN  if stats and stats.count else TEXT_DIM)
            _cell(min_str,       50, fg=SIG_RED    if stats and stats.count else TEXT_DIM)
            _cell(avg_str,       50, fg=_sig_color(stats.avg_dbm) if stats and stats.count else TEXT_DIM)

            # Hover highlight
            def _on_enter(e, r=row, bg=row_bg):
                for w in r.winfo_children():
                    try: w.config(bg="#1e1e3a")
                    except: pass
                r.config(bg="#1e1e3a")
            def _on_leave(e, r=row, bg=row_bg):
                for w in r.winfo_children():
                    try: w.config(bg=bg)
                    except: pass
                r.config(bg=bg)
            row.bind("<Enter>", _on_enter)
            row.bind("<Leave>", _on_leave)

            self._ap_row_widgets.append({"bssid": ap.bssid, "check_var": check_var, "row": row})

        self.ap_rows_frame.update_idletasks()
        self.ap_canvas.configure(scrollregion=self.ap_canvas.bbox("all"))

    def _draw_signal_bar(self, parent: tk.Frame, dbm: int, bg_color: str):
        """Draw a small colored bar chart showing signal strength."""
        c = tk.Canvas(parent, bg=bg_color, highlightthickness=0,
                      width=108, height=22)
        c.pack(fill="both", expand=True)

        color = _sig_color(dbm)
        pct   = max(0, min(100, (dbm + 90) * 100 // 60))
        bar_w = int(pct * 68 / 100)

        # Background track
        c.create_rectangle(2, 6, 72, 18, fill="#1a1a30", outline="")
        # Signal bar
        if bar_w > 0:
            c.create_rectangle(2, 6, 2 + bar_w, 18, fill=color, outline="")
        # dBm label
        c.create_text(80, 12, text=f"{dbm}", font=("Courier", 7, "bold"),
                      fill=color, anchor="w")

    def _on_ap_toggle(self, bssid: str, var: tk.BooleanVar):
        if var.get():
            self.selected_bssids.add(bssid)
        else:
            self.selected_bssids.discard(bssid)
        self._sync_heatmap_bssids()
        self._update_heatmap_ap_selector()

    def _ap_select_all(self):
        self.selected_bssids = {ap.bssid for ap in self.last_scan}
        for row in self._ap_row_widgets:
            row["check_var"].set(True)
        self._sync_heatmap_bssids()
        self._update_heatmap_ap_selector()

    def _ap_deselect_all(self):
        self.selected_bssids.clear()
        for row in self._ap_row_widgets:
            row["check_var"].set(False)
        self._sync_heatmap_bssids()
        self._update_heatmap_ap_selector()

    def _sync_heatmap_bssids(self):
        """Keep selected_bssids_for_heatmap in sync with the AP tab checkboxes.
        Preserves a stable order: scan order first, then session-only BSSIDs."""
        ordered = [ap.bssid for ap in self.last_scan if ap.bssid in self.selected_bssids]
        scan_set = {ap.bssid for ap in self.last_scan}
        for bssid in self.session.get_all_bssids():
            if bssid in self.selected_bssids and bssid not in scan_set:
                ordered.append(bssid)
        self.selected_bssids_for_heatmap = ordered

    # ── Heatmap Tab ───────────────────────────────────────────────────────────

    def _build_heatmap_tab(self):
        f = self.heatmap_tab

        # Toolbar
        toolbar = tk.Frame(f, bg=BG3, height=48)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew")
        toolbar.grid_columnconfigure(99, weight=1)

        tk.Label(toolbar, text="  Click canvas to record a measurement",
                 font=("Courier", 9), bg=BG3, fg=TEXT_DIM).grid(row=0, column=0, padx=8, pady=12, sticky="w")

        self.scan_btn = tk.Button(toolbar, text="▶  Scan & Place",
            font=("Courier", 9, "bold"), bg=ACCENT, fg="white",
            relief="flat", padx=12, pady=6, cursor="hand2",
            activebackground="#5590ff", activeforeground="white",
            command=self._begin_placement_mode)
        self.scan_btn.grid(row=0, column=1, padx=6, pady=8)

        self.undo_btn = tk.Button(toolbar, text="↩  Undo",
            font=("Courier", 9), bg=BG2, fg=TEXT,
            relief="flat", padx=10, pady=6, cursor="hand2",
            command=self._undo_last)
        self.undo_btn.grid(row=0, column=2, padx=4, pady=8)

        self.status_label = tk.Label(toolbar, text="Ready",
            font=("Courier", 9), bg=BG3, fg=TEXT_DIM)
        self.status_label.grid(row=0, column=99, padx=12, sticky="e")

        # Canvas area
        self.fig = Figure(figsize=(9, 6), dpi=100)
        self.fig.patch.set_facecolor(BG)
        self.ax  = self.fig.add_subplot(111)
        self.ax.set_facecolor(BG)

        self.canvas_widget = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas_widget.get_tk_widget().grid(row=1, column=0, sticky="nsew")
        self.canvas_widget.mpl_connect("button_press_event", self._on_canvas_click)
        f.columnconfigure(0, weight=1)

        # Right panel — AP selector + measurements (full window height)
        right = tk.Frame(f, bg=BG2, width=260)
        right.grid(row=1, column=1, sticky="nsew", padx=(1, 0))
        right.grid_propagate(False)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)   # measurements listbox expands
        self._build_heatmap_right(right)

        self._placement_mode = False
        self._pending_scan: Optional[list[AccessPoint]] = None

    def _build_heatmap_right(self, f: tk.Frame):
        # ── Active AP display ─────────────────────────────────────────────────
        tk.Label(f, text="ACTIVE NETWORKS", font=("Courier", 8, "bold"),
                 bg=BG2, fg=TEXT_DIM).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 2))

        tk.Label(f,
            text="Check networks on the Access\nPoints tab to include them here.\n"
                 "1 selected = single BSSID mode.\n"
                 "2+ selected = averaged mode.",
            font=("Courier", 7), bg=BG2, fg=TEXT_DIM, justify="left", wraplength=230
        ).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 6))

        # Scrollable list showing the currently active BSSIDs
        active_frame = tk.Frame(f, bg=BG3, relief="flat",
                                highlightthickness=1,
                                highlightbackground=BORDER)
        active_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        active_frame.columnconfigure(0, weight=1)

        active_scroll = ttk.Scrollbar(active_frame, orient="vertical")
        active_scroll.grid(row=0, column=1, sticky="ns")

        self.active_bssid_listbox = tk.Listbox(
            active_frame,
            font=("Courier", 7), bg=BG3, fg=ACCENT2,
            relief="flat", borderwidth=0,
            highlightthickness=0,
            activestyle="none", height=5,
            selectbackground=BG3, selectforeground=ACCENT2,
            yscrollcommand=active_scroll.set)
        self.active_bssid_listbox.grid(row=0, column=0, sticky="ew")
        active_scroll.config(command=self.active_bssid_listbox.yview)

        self.heatmap_mode_label = tk.Label(f, text="",
            font=("Courier", 7, "bold"), bg=BG2, fg=WARNING,
            wraplength=230, justify="left")
        self.heatmap_mode_label.grid(row=3, column=0, sticky="w", padx=14, pady=(2, 4))

        ttk.Separator(f, orient="horizontal").grid(row=4, column=0, sticky="ew", padx=8, pady=6)

        # ── Measurements list (full remaining height) ──────────────────────
        tk.Label(f, text="MEASUREMENTS", font=("Courier", 8, "bold"),
                 bg=BG2, fg=TEXT_DIM).grid(row=5, column=0, sticky="w", padx=14, pady=(4, 2))

        self.meas_count_label = tk.Label(f, text="0 points collected",
            font=("Courier", 8), bg=BG2, fg=ACCENT2)
        self.meas_count_label.grid(row=6, column=0, sticky="w", padx=14, pady=(0, 4))

        meas_frame = tk.Frame(f, bg=BG2)
        meas_frame.grid(row=7, column=0, sticky="nsew", padx=8, pady=(0, 4))
        meas_frame.columnconfigure(0, weight=1)
        meas_frame.rowconfigure(0, weight=1)
        f.rowconfigure(7, weight=1)

        meas_scroll = ttk.Scrollbar(meas_frame, orient="vertical")
        meas_scroll.grid(row=0, column=1, sticky="ns")

        self.meas_listbox = tk.Listbox(meas_frame,
            font=("Courier", 8),
            bg=BG3, fg=TEXT_DIM, selectbackground=DANGER,
            relief="flat", borderwidth=0,
            highlightthickness=1, highlightcolor=BORDER, highlightbackground=BORDER,
            activestyle="none",
            yscrollcommand=meas_scroll.set)
        self.meas_listbox.grid(row=0, column=0, sticky="nsew")
        meas_scroll.config(command=self.meas_listbox.yview)

        self.del_meas_btn = tk.Button(f, text="🗑  Delete Selected",
            font=("Courier", 8), bg=BG3, fg=DANGER,
            relief="flat", padx=8, pady=4, cursor="hand2",
            command=self._delete_measurement)
        self.del_meas_btn.grid(row=8, column=0, sticky="w", padx=12, pady=(0, 10))

    # ── Active BSSID display on Heatmap tab ───────────────────────────────────

    def _update_heatmap_ap_selector(self):
        """Reflect the current AP tab selection in the Heatmap tab status panel."""
        self._sync_heatmap_bssids()
        bssids = self.selected_bssids_for_heatmap

        self.active_bssid_listbox.delete(0, tk.END)

        if not bssids:
            self.active_bssid_listbox.insert(tk.END, "  — none selected —")
            self.heatmap_mode_label.config(
                text="⚠ No networks selected.\nGo to Access Points tab and\ncheck at least one.",
                fg=DANGER)
        else:
            for bssid in bssids:
                ssid = self.session.get_ssid(bssid)
                self.active_bssid_listbox.insert(tk.END, f"  {ssid}  {bssid}")
            if len(bssids) == 1:
                self.heatmap_mode_label.config(
                    text=f"Mode: single BSSID", fg=ACCENT2)
            else:
                self.heatmap_mode_label.config(
                    text=f"Mode: averaging {len(bssids)} BSSIDs", fg=WARNING)

        self._redraw()

    # ── Scanning (AP Tab) ─────────────────────────────────────────────────────

    def _refresh_scan(self):
        if self.scanning: return
        self.scanning = True
        self.ap_scan_btn.config(text="⏳  Scanning…", state="disabled")
        self.ap_status_label.config(text="Scanning…", fg=WARNING)
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        try:
            aps = scan_averaged(samples=5, delay=0.4)
            self.root.after(0, lambda: self._scan_complete(aps))
        except Exception as e:
            self.root.after(0, lambda: self._scan_error(str(e)))

    def _scan_complete(self, aps: list[AccessPoint]):
        self.scanning = False
        self.last_scan = aps
        self.ap_scan_btn.config(text="🔄  Refresh Scan", state="normal")

        # Accumulate running stats for every AP seen in this scan
        for ap in aps:
            if ap.bssid not in self._ap_stats:
                self._ap_stats[ap.bssid] = _ApStats()
            self._ap_stats[ap.bssid].update(ap.signal_dbm)

        self.ap_status_label.config(
            text=f"Found {len(aps)} network{'s' if len(aps) != 1 else ''}",
            fg=SUCCESS)
        # Auto-select all on first scan
        if not self.selected_bssids:
            self.selected_bssids = {ap.bssid for ap in aps}
        self._populate_ap_table(aps)
        self._sync_heatmap_bssids()
        self._update_heatmap_ap_selector()
        self._set_status(f"Scan complete — {len(aps)} networks found")

        # Schedule next auto-scan if enabled
        if self._auto_scan_enabled:
            self._schedule_auto_scan()

    def _scan_error(self, err: str):
        self.scanning = False
        self.ap_scan_btn.config(text="🔄  Refresh Scan", state="normal")
        self.ap_status_label.config(text="Scan failed", fg=DANGER)
        self._set_status(f"Scan failed: {err}", error=True)
        # Keep auto-scanning even after an error
        if self._auto_scan_enabled:
            self._schedule_auto_scan()
        messagebox.showerror("Scan Error",
            f"WiFi scan failed:\n{err}\n\n"
            "On Linux, try: sudo nmcli dev wifi list --rescan yes\n"
            "On Windows, run as Administrator if needed.")

    # ── Auto-scan ─────────────────────────────────────────────────────────────

    def _toggle_auto_scan(self):
        self._auto_scan_enabled = self._auto_scan_var.get()
        if self._auto_scan_enabled:
            self._schedule_auto_scan()
            self.ap_scan_btn.config(state="disabled")   # manual scan disabled while auto is on
        else:
            self._cancel_auto_scan()
            self.ap_scan_btn.config(state="normal")
            self.auto_scan_countdown.config(text="")

    def _on_interval_change(self, event=None):
        """User changed the interval spinbox — restart the countdown if running."""
        if self._auto_scan_enabled:
            self._cancel_auto_scan()
            self._schedule_auto_scan()

    def _schedule_auto_scan(self):
        """Start the countdown ticker then fire the scan at the end."""
        self._cancel_auto_scan()
        interval = max(3, self._auto_scan_interval.get())
        self._auto_scan_remaining = interval
        self._tick_countdown()

    def _cancel_auto_scan(self):
        if self._auto_scan_job:
            self.root.after_cancel(self._auto_scan_job)
            self._auto_scan_job = None

    def _tick_countdown(self):
        """Called every second to update the countdown label."""
        if not self._auto_scan_enabled:
            self.auto_scan_countdown.config(text="")
            return
        if self._auto_scan_remaining <= 0:
            self.auto_scan_countdown.config(text="scanning…", fg=WARNING)
            self._fire_auto_scan()
            return
        self.auto_scan_countdown.config(
            text=f"next scan in {self._auto_scan_remaining}s", fg=TEXT_DIM)
        self._auto_scan_remaining -= 1
        self._auto_scan_job = self.root.after(1000, self._tick_countdown)

    def _fire_auto_scan(self):
        """Trigger the actual scan for the auto-scan cycle."""
        if self.scanning:
            # A placement scan is in progress — reschedule without firing
            self._schedule_auto_scan()
            return
        self.scanning = True
        self.ap_status_label.config(text="Auto-scanning…", fg=WARNING)
        threading.Thread(target=self._do_scan, daemon=True).start()

    # ── Scanning (Heatmap placement) ──────────────────────────────────────────

    def _begin_placement_mode(self):
        if self.scanning: return
        if not self.selected_bssids_for_heatmap:
            messagebox.showinfo("No APs Selected",
                "Please select at least one access point\non the Access Points tab first.")
            self.notebook.select(self.ap_tab)
            return
        self.scanning = True
        self.scan_btn.config(text="⏳ Scanning…", state="disabled")
        self._set_status("Scanning… click your position on the map when ready.")
        threading.Thread(target=self._scan_for_placement, daemon=True).start()

    def _scan_for_placement(self):
        try:
            aps = scan_averaged(samples=6, delay=0.4)
            self.root.after(0, lambda: self._ready_to_place(aps))
        except Exception as e:
            self.root.after(0, lambda: self._scan_error(str(e)))

    def _ready_to_place(self, aps: list[AccessPoint]):
        self.scanning = False
        self._pending_scan = aps
        self._placement_mode = True
        # Also refresh AP tab
        if aps:
            for ap in aps:
                if ap.bssid not in {r["bssid"] for r in self._ap_row_widgets}:
                    pass  # new APs from this scan; full repopulate next refresh
        self.scan_btn.config(text="📍 Click your location", state="normal", bg=ACCENT2)
        self._set_status("✓ Scan done — click your position on the map")
        self.canvas_widget.get_tk_widget().config(cursor="crosshair")

    def _on_canvas_click(self, event):
        if event.inaxes != self.ax: return
        if not self._placement_mode or not self._pending_scan: return
        x, y = event.xdata, event.ydata
        if x is None or y is None: return

        signals  = {ap.bssid: ap.signal_dbm for ap in self._pending_scan}
        ssid_map = {ap.bssid: ap.ssid       for ap in self._pending_scan}

        self.session.add_measurement(Measurement(x=x, y=y, signals=signals, ssid_map=ssid_map))

        self._placement_mode = False
        self._pending_scan   = None
        self.scan_btn.config(text="▶  Scan & Place", bg=ACCENT)
        self.canvas_widget.get_tk_widget().config(cursor="")

        n = len(self.session.measurements)
        self._set_status(f"✓ Point #{n} recorded at ({int(x)}, {int(y)})")
        self._refresh_everything()

    def _undo_last(self):
        if not self.session.measurements: return
        self.session.remove_measurement(len(self.session.measurements) - 1)
        self._set_status("Last measurement removed")
        self._refresh_everything()

    def _delete_measurement(self):
        sel = self.meas_listbox.curselection()
        if not sel: return
        idx = sel[0]
        self.session.remove_measurement(idx)
        self._refresh_everything()
        self._set_status(f"Measurement {idx + 1} deleted")

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _redraw(self):
        if self.selected_bssids_for_heatmap:
            try:
                render_heatmap(self.session, self.selected_bssids_for_heatmap, self.fig, self.ax)
            except Exception as e:
                self.ax.clear()
                self.ax.text(0.5, 0.5, f"Render error:\n{e}",
                             transform=self.ax.transAxes, ha="center", va="center",
                             color="red", fontsize=10)
        else:
            self._draw_empty_canvas()
        self.canvas_widget.draw()

    def _draw_empty_canvas(self):
        self.ax.clear()
        self.ax.set_facecolor(BG)
        self.fig.patch.set_facecolor(BG)
        w, h = self.session.canvas_width, self.session.canvas_height

        if self.session.floorplan_path:
            from PIL import Image
            try:
                img = Image.open(self.session.floorplan_path).convert("RGBA")
                img = img.resize((w, h), Image.LANCZOS)
                self.ax.imshow(np.array(img), extent=[0, w, h, 0], aspect="auto", alpha=0.4)
            except Exception:
                pass
        else:
            spacing = max(w, h) // 10
            for x in range(0, w + 1, spacing):
                self.ax.axvline(x, color="#1e2a4a", linewidth=0.5)
            for y in range(0, h + 1, spacing):
                self.ax.axhline(y, color="#1e2a4a", linewidth=0.5)

        if not self.selected_bssids_for_heatmap:
            msg = "← Select networks on the\nAccess Points tab first"
        else:
            n = len(self.selected_bssids_for_heatmap)
            msg = (f"{n} BSSID{'s' if n > 1 else ''} selected.\n"
                   f"Click  ▶ Scan & Place  to begin.")
            if n > 1:
                msg = f"Averaging {n} BSSIDs.\n" + msg
        if not self.session.floorplan_path:
            msg += "\n\n⚠ No floorplan — adding one\nincreases accuracy."

        self.ax.text(w / 2, h / 2, msg, ha="center", va="center",
                     color="#4a4a7a", fontsize=12, linespacing=1.8,
                     bbox=dict(boxstyle="round,pad=0.8", facecolor="#10101e", alpha=0.7))
        self.ax.set_xlim(0, w); self.ax.set_ylim(h, 0)
        self.ax.set_xticks([]); self.ax.set_yticks([])

    # ── Session management ────────────────────────────────────────────────────

    def _new_session(self):
        if self.session.measurements:
            if not messagebox.askyesno("New Session", "Discard current session?"): return
        self.session = Session(name="New Session")
        self.session_path = None
        self.selected_bssids_for_heatmap = []
        self._ap_stats.clear()
        self._refresh_everything()
        self._set_status("New session started")

    def _open_session(self):
        path = filedialog.askopenfilename(title="Open Session",
            filetypes=[("Heatmap Session", "*.json"), ("All Files", "*.*")])
        if path: self._load_session(path)

    def _load_session(self, path: str):
        try:
            self.session = Session.load(path)
            self.session_path = path
            self._refresh_everything()
            self._set_status(f"Loaded: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Load Error", str(e))

    def _save_session(self):
        if not self.session_path: self._save_session_as(); return
        try:
            self.session.save(self.session_path)
            self._set_status(f"Saved: {os.path.basename(self.session_path)}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _save_session_as(self):
        path = filedialog.asksaveasfilename(title="Save Session As",
            defaultextension=".json", filetypes=[("Heatmap Session", "*.json")])
        if path:
            self.session_path = path
            self._save_session()

    def _load_floorplan(self):
        path = filedialog.askopenfilename(title="Load Floorplan Image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.tiff"), ("All Files", "*.*")])
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
            self.floorplan_label.config(text=f"✓ {os.path.basename(self.session.floorplan_path)}", fg=SUCCESS)
            self.floorplan_note.config(text="")
        else:
            self.floorplan_label.config(text="None  (grid mode)", fg=WARNING)
            self.floorplan_note.config(text="⚠ Adding a floorplan\nincreases accuracy.")

    def _export_png(self):
        if not self.selected_bssids_for_heatmap:
            messagebox.showinfo("Export", "Select at least one access point first."); return
        path = filedialog.asksaveasfilename(title="Export Heatmap",
            defaultextension=".png",
            filetypes=[("PNG Image", "*.png"), ("JPEG Image", "*.jpg")])
        if path:
            try:
                render_heatmap(self.session, self.selected_bssids_for_heatmap, export_path=path)
                self._set_status(f"Exported: {os.path.basename(path)}")
                messagebox.showinfo("Export", f"Saved to:\n{path}")
            except Exception as e:
                messagebox.showerror("Export Error", str(e))

    # ── Refresh helpers ───────────────────────────────────────────────────────

    def _refresh_everything(self):
        self._update_measurements_list()
        self._update_session_info()
        self._update_floorplan_label()
        self._update_heatmap_ap_selector()
        self._redraw()

    def _update_measurements_list(self):
        self.meas_listbox.delete(0, tk.END)
        for i, m in enumerate(self.session.measurements):
            self.meas_listbox.insert(
                tk.END, f"#{i+1:>3}  ({int(m.x):>4}, {int(m.y):>4})  {len(m.signals)} APs")
        count = len(self.session.measurements)
        self.meas_count_label.config(
            text=f"{count} point{'s' if count != 1 else ''} collected")

    def _update_session_info(self):
        fp   = "✓ floorplan" if self.session.floorplan_path else "grid mode"
        path = os.path.basename(self.session_path) if self.session_path else "unsaved"
        self.session_info.config(
            text=f"Session: {self.session.name}\nFile: {path}\nMode: {fp}")

    def _set_status(self, msg: str, error: bool = False):
        self.status_label.config(text=msg, fg=DANGER if error else TEXT_DIM)

    # ── Styles ────────────────────────────────────────────────────────────────

    def _apply_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TSeparator", background=BORDER)
        style.configure("TScrollbar",
                         background=BG3, troughcolor=BG2,
                         bordercolor=BORDER, arrowcolor=TEXT_DIM)
        style.configure("TNotebook",
                         background=BG2, borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab",
                         background=BG3, foreground=TEXT_DIM,
                         font=("Courier", 9, "bold"),
                         padding=(14, 8), borderwidth=0)
        style.map("TNotebook.Tab",
                   background=[("selected", BG)],
                   foreground=[("selected", ACCENT2)])
        style.configure("TCombobox",
                         fieldbackground=BG3, background=BG3,
                         foreground=TEXT, selectbackground=ACCENT,
                         bordercolor=BORDER, arrowcolor=TEXT_DIM)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WiFi Heatmap Generator")
    parser.add_argument("--session", help="Path to a .json session file to load on startup")
    args = parser.parse_args()

    root = tk.Tk()
    root.title("WiFi Heatmap Generator")
    try:
        root.iconbitmap(default="")
    except Exception:
        pass

    WiFiHeatmapApp(root, initial_session=args.session)
    root.protocol("WM_DELETE_WINDOW", lambda: (root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
