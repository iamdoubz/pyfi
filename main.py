"""
main.py — pyFi WiFi Heatmap Generator GUI
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
import sys
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


def _resource_path(relative_path: str) -> str:
    """
    Resolve a path to a bundled resource at runtime.

    PyInstaller extracts bundled files to a temporary folder at runtime and
    sets sys._MEIPASS to that folder's path. When running from source,
    the directory containing main.py is used instead — so both the compiled
    binary and plain `python main.py` resolve assets correctly.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


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
        self.root.title("pyFi")
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

        # AP position drag state
        self._ap_drag_mode: bool = False        # True when "Place APs" is active
        self._dragging_bssid: Optional[str] = None
        self._drag_ghost = None                  # matplotlib artist for drag preview

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

        # Logo image — load pyfi-mini.png, fall back to emoji if file is missing
        try:
            _logo_img = tk.PhotoImage(file=_resource_path(
                os.path.join("extras", "pyfi-mini.png")))
            # Use a single divisor for both axes to preserve aspect ratio.
            # Target ~64px on the longest side.
            _divisor = max(1, max(_logo_img.width(), _logo_img.height()) // 92)
            _logo_img = _logo_img.subsample(_divisor, _divisor)
            tk.Label(f, image=_logo_img, bg=BG2).pack(pady=(20, 4))
            f._logo_img_ref = _logo_img   # prevent garbage collection
        except Exception:
            tk.Label(f, text="📡", font=("Segoe UI Emoji", 28), bg=BG2, fg=ACCENT).pack(pady=(20, 4))

        tk.Label(f, text="WiFi", font=("Georgia", 13, "bold"), bg=BG2, fg=TEXT).pack()
        tk.Label(f, text="Heatmap Generator", font=("Georgia", 10), bg=BG2, fg=TEXT_DIM).pack(pady=(0, 16))

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

        # ── Toolbar ───────────────────────────────────────────────────────────
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

        # Auto-scan controls row
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

        # ── Scrollable table (header + rows share one Canvas) ─────────────────
        # Both the sticky header row and every data row are child frames of a
        # single inner container that sits inside the Canvas window. This
        # guarantees horizontal scroll moves header and data identically, and
        # that pixel-exact column widths produce perfect alignment.
        table_outer = tk.Frame(f, bg=BG)
        table_outer.grid(row=1, column=0, sticky="nsew")
        table_outer.columnconfigure(0, weight=1)
        table_outer.rowconfigure(0, weight=1)

        self.ap_canvas = tk.Canvas(table_outer, bg=BG, highlightthickness=0)
        ap_vscroll = ttk.Scrollbar(table_outer, orient="vertical",   command=self.ap_canvas.yview)
        ap_hscroll = ttk.Scrollbar(table_outer, orient="horizontal", command=self.ap_canvas.xview)

        self.ap_canvas.configure(yscrollcommand=ap_vscroll.set,
                                  xscrollcommand=ap_hscroll.set)
        self.ap_canvas.grid(row=0, column=0, sticky="nsew")
        ap_vscroll.grid(row=0, column=1, sticky="ns")
        ap_hscroll.grid(row=1, column=0, sticky="ew")

        # Single inner frame: row 0 = header, rows 1+ = data
        self.ap_inner = tk.Frame(self.ap_canvas, bg=BG)
        self.ap_canvas_window = self.ap_canvas.create_window(
            (0, 0), window=self.ap_inner, anchor="nw")

        self.ap_inner.bind("<Configure>", self._on_ap_frame_configure)
        self.ap_canvas.bind("<Configure>", self._on_ap_canvas_configure)
        self.ap_canvas.bind("<MouseWheel>", lambda e: self.ap_canvas.yview_scroll(-1*(e.delta//120), "units"))
        self.ap_canvas.bind("<Button-4>",   lambda e: self.ap_canvas.yview_scroll(-1, "units"))
        self.ap_canvas.bind("<Button-5>",   lambda e: self.ap_canvas.yview_scroll( 1, "units"))

        # Build the header as row 0 inside ap_inner
        self._build_ap_header(self.ap_inner)

        # Row data storage
        self._ap_row_widgets: list[dict] = []

    # ── Column geometry ───────────────────────────────────────────────────────
    # We use fixed pixel widths throughout. CELL_PAD is the total horizontal
    # padding inside every cell (left + right). Every cell widget is given
    # width=col_px so the header and data columns are identical in size.
    CELL_PAD   = 8    # px padding on each side inside a cell label
    ROW_HEIGHT = 28   # px height for every row including header

    def _col_px(self, width_hint: int) -> int:
        """Return the actual pixel width for a column given its hint."""
        return width_hint  # hints are already in pixels; no scaling needed

    def _build_ap_header(self, parent: tk.Frame):
        """Build the header as the first row of the inner frame."""
        hdr = tk.Frame(parent, bg=BG3)
        hdr.grid(row=0, column=0, sticky="ew")

        # Checkbox placeholder — same width as the actual Checkbutton (26px)
        tk.Label(hdr, text="", bg=BG3, width=3).pack(side="left", padx=(2, 0))

        for col_id, label, width in AP_COLUMNS:
            if col_id == "sel":
                continue   # handled by the placeholder above
            tk.Label(hdr, text=label,
                     font=("Courier", 8, "bold"),
                     bg=BG3, fg=TEXT_DIM,
                     width=0,          # let wraplength/padx control width
                     anchor="w",
                     padx=self.CELL_PAD // 2,
                     pady=6
                     ).pack(side="left",
                            ipadx=0,
                            # Force exact pixel width via a fixed-size frame wrapper
                            )
            # Use a fixed-pixel container frame so the label is always exactly
            # `width` pixels wide — matching the data cells below.
            hdr.pack_slaves()[-1].pack_forget()
            container = tk.Frame(hdr, bg=BG3, width=width, height=self.ROW_HEIGHT)
            container.pack(side="left")
            container.pack_propagate(False)
            tk.Label(container, text=label,
                     font=("Courier", 8, "bold"),
                     bg=BG3, fg=TEXT_DIM,
                     anchor="w", padx=4).place(relwidth=1, relheight=1)

    def _on_ap_frame_configure(self, event):
        self.ap_canvas.configure(scrollregion=self.ap_canvas.bbox("all"))

    def _on_ap_canvas_configure(self, event):
        # Do NOT force the inner frame to the canvas width — that would
        # suppress horizontal scrolling. Let it be its natural width.
        pass

    def _known_bssids_ordered(self) -> list[str]:
        """
        Return all BSSIDs ever seen, in a stable display order:
        currently-visible APs first (sorted by signal desc), then
        previously-seen-but-now-absent APs (sorted by last_seen desc).
        """
        current_bssids = [ap.bssid for ap in self.last_scan]
        current_set    = set(current_bssids)
        stale_bssids   = sorted(
            [b for b in self._ap_stats if b not in current_set],
            key=lambda b: self._ap_stats[b].last_seen_ts,
            reverse=True
        )
        return current_bssids + stale_bssids

    def _populate_ap_table(self, aps: list[AccessPoint]):
        """
        Rebuild data rows inside ap_inner (below the sticky header).
        Shows every BSSID ever seen — current ones at full brightness,
        stale ones (absent from latest scan) dimmed with a 'last seen' note.
        """
        # Remove old data rows (keep row 0 = header)
        for widget in list(self.ap_inner.grid_slaves()):
            info = widget.grid_info()
            if info.get("row", 0) > 0:
                widget.destroy()
        self._ap_row_widgets.clear()

        # Build a lookup map for current scan
        current_map: dict[str, AccessPoint] = {ap.bssid: ap for ap in aps}
        now         = time.monotonic()
        all_bssids  = self._known_bssids_ordered()

        for i, bssid in enumerate(all_bssids):
            ap      = current_map.get(bssid)
            stats   = self._ap_stats.get(bssid)
            is_live = ap is not None
            row_num = i + 1   # row 0 is the header

            row_bg  = BG if i % 2 == 0 else BG2
            dim_fg  = "#3a3a5a"   # very dim — used for stale rows

            row = tk.Frame(self.ap_inner, bg=row_bg)
            row.grid(row=row_num, column=0, sticky="ew")

            # ── Checkbox ──────────────────────────────────────────────────────
            check_var = tk.BooleanVar(value=(bssid in self.selected_bssids))
            tk.Checkbutton(row, variable=check_var, bg=row_bg,
                           activebackground=row_bg, fg=ACCENT2, selectcolor=BG3,
                           relief="flat", cursor="hand2",
                           command=lambda b=bssid, v=check_var: self._on_ap_toggle(b, v)
                           ).pack(side="left", padx=(4, 0))

            def _cell(text, width, fg=TEXT, bold=False):
                """Pack a fixed-pixel-width cell."""
                container = tk.Frame(row, bg=row_bg, width=width, height=self.ROW_HEIGHT)
                container.pack(side="left")
                container.pack_propagate(False)
                tk.Label(container, text=str(text),
                         font=("Courier", 8, "bold") if bold else ("Courier", 8),
                         bg=row_bg, fg=fg,
                         anchor="w", padx=4).place(relwidth=1, relheight=1)

            # Resolve display values — use last-known from stats when AP is stale
            ssid     = ap.ssid if ap else self._ap_stats_ssid(bssid)
            channel  = str(ap.channel) if ap and ap.channel else "—"
            freq     = ap.freq_label()     if ap else "—"
            width_s  = ap.channel_width or "—" if ap else "—"
            band     = ap.band or "—"      if ap else "—"
            security = ap.security or "—"  if ap else "—"
            vendor   = ap.vendor or "—"    if ap else "—"
            mode     = ap.mode_label()     if ap else "—"

            text_fg  = TEXT     if is_live else dim_fg
            dim2     = TEXT_DIM if is_live else dim_fg

            _cell(ssid,       160, fg=text_fg, bold=True)
            _cell(bssid,      145, fg=dim2)
            _cell(channel,     38, fg=text_fg)
            _cell(freq,        90, fg=text_fg)
            _cell(width_s,     75, fg=text_fg)
            _cell(band,        60, fg=ACCENT2 if is_live else dim_fg)
            _cell(security,   160, fg=text_fg)
            _cell(vendor,      90, fg=dim2)
            _cell(mode,        90, fg=text_fg)

            # Signal bar — live APs get a colored bar; stale get a dim placeholder
            bar_w = 110
            bar_container = tk.Frame(row, bg=row_bg, width=bar_w, height=self.ROW_HEIGHT)
            bar_container.pack(side="left")
            bar_container.pack_propagate(False)
            if is_live:
                self._draw_signal_bar(bar_container, ap.signal_dbm, row_bg)
            else:
                tk.Label(bar_container, text="out of range",
                         font=("Courier", 7), bg=row_bg, fg=dim_fg,
                         anchor="w", padx=4).place(relwidth=1, relheight=1)

            # Stat columns
            if stats and stats.count > 0:
                ls_str  = stats.last_seen_str(now)
                max_str = f"{stats.max_dbm}"
                min_str = f"{stats.min_dbm}"
                avg_str = f"{stats.avg_dbm}"
                _cell(ls_str,  75, fg=TEXT_DIM if is_live else dim_fg)
                _cell(max_str, 50, fg=SIG_GREEN if is_live else dim_fg)
                _cell(min_str, 50, fg=SIG_RED   if is_live else dim_fg)
                _cell(avg_str, 50, fg=_sig_color(stats.avg_dbm) if is_live else dim_fg)
            else:
                for w in (75, 50, 50, 50):
                    _cell("—", w, fg=dim_fg)

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

            self._ap_row_widgets.append({"bssid": bssid, "check_var": check_var, "row": row})

        self.ap_inner.update_idletasks()
        self.ap_canvas.configure(scrollregion=self.ap_canvas.bbox("all"))

    def _ap_stats_ssid(self, bssid: str) -> str:
        """Look up SSID for a stale BSSID from session data or return the BSSID itself."""
        ssid = self.session.get_ssid(bssid)
        return ssid if ssid != bssid else bssid

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
        self.selected_bssids = {r["bssid"] for r in self._ap_row_widgets}
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

        # Place APs toggle — activates drag-and-drop mode for positioning APs
        self.place_ap_btn = tk.Button(toolbar, text="◆  Place APs",
            font=("Courier", 9), bg=BG2, fg=ACCENT2,
            relief="flat", padx=10, pady=6, cursor="hand2",
            activebackground=BG3,
            command=self._toggle_ap_drag_mode)
        self.place_ap_btn.grid(row=0, column=3, padx=4, pady=8)

        self.clear_ap_pos_btn = tk.Button(toolbar, text="✕  Clear AP Pins",
            font=("Courier", 9), bg=BG2, fg=TEXT_DIM,
            relief="flat", padx=10, pady=6, cursor="hand2",
            command=self._clear_ap_positions)
        self.clear_ap_pos_btn.grid(row=0, column=4, padx=4, pady=8)

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
        self.canvas_widget.mpl_connect("button_press_event",   self._on_canvas_click)
        self.canvas_widget.mpl_connect("motion_notify_event",  self._on_canvas_motion)
        self.canvas_widget.mpl_connect("button_release_event", self._on_canvas_release)
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
                 "2+ selected = averaged mode.\n"
                 "Enable ◆ Place APs then drag\na network here onto the map.",
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

        # ── AP Positions panel ─────────────────────────────────────────────────
        tk.Label(f, text="AP POSITIONS  ◆", font=("Courier", 8, "bold"),
                 bg=BG2, fg=TEXT_DIM).grid(row=5, column=0, sticky="w", padx=14, pady=(4, 2))

        tk.Label(f,
            text="Click ◆ Place APs, then click a\nnetwork below to arm it, then\n"
                 "click its location on the map.\nRight-click a ◆ marker to remove.",
            font=("Courier", 7), bg=BG2, fg=TEXT_DIM, justify="left", wraplength=230
        ).grid(row=6, column=0, sticky="w", padx=14, pady=(0, 4))

        # Frame that holds one row per active BSSID
        self.ap_pos_frame = tk.Frame(f, bg=BG2)
        self.ap_pos_frame.grid(row=7, column=0, sticky="ew", padx=8, pady=(0, 4))
        self.ap_pos_frame.columnconfigure(0, weight=1)

        ttk.Separator(f, orient="horizontal").grid(row=8, column=0, sticky="ew", padx=8, pady=6)

        # ── Measurements list (full remaining height) ──────────────────────
        tk.Label(f, text="MEASUREMENTS", font=("Courier", 8, "bold"),
                 bg=BG2, fg=TEXT_DIM).grid(row=9, column=0, sticky="w", padx=14, pady=(4, 2))

        self.meas_count_label = tk.Label(f, text="0 points collected",
            font=("Courier", 8), bg=BG2, fg=ACCENT2)
        self.meas_count_label.grid(row=10, column=0, sticky="w", padx=14, pady=(0, 4))

        meas_frame = tk.Frame(f, bg=BG2)
        meas_frame.grid(row=11, column=0, sticky="nsew", padx=8, pady=(0, 4))
        meas_frame.columnconfigure(0, weight=1)
        meas_frame.rowconfigure(0, weight=1)
        f.rowconfigure(11, weight=1)

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
        self.del_meas_btn.grid(row=12, column=0, sticky="w", padx=12, pady=(0, 10))

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

    def _update_ap_positions_panel(self, highlight: Optional[str] = None):
        """
        Rebuild the AP Positions panel rows — one row per active BSSID.
        Each row shows: armed indicator | SSID | pinned coords | remove button.
        Clicking the row arms that BSSID for placement.
        """
        for w in self.ap_pos_frame.winfo_children():
            w.destroy()

        bssids = self.selected_bssids_for_heatmap
        if not bssids:
            tk.Label(self.ap_pos_frame, text="  No networks selected",
                     font=("Courier", 7), bg=BG2, fg=TEXT_DIM
                     ).grid(row=0, column=0, sticky="w", padx=8, pady=2)
            return

        for i, bssid in enumerate(bssids):
            ssid    = self.session.get_ssid(bssid)
            pos     = self.session.ap_positions.get(bssid)
            is_armed = (bssid == self._dragging_bssid and self._ap_drag_mode)
            is_pinned = pos is not None

            row_bg  = "#1a2a1a" if is_armed else (BG3 if is_pinned else BG2)
            row     = tk.Frame(self.ap_pos_frame, bg=row_bg,
                               highlightthickness=1,
                               highlightbackground=ACCENT2 if is_armed else BORDER)
            row.grid(row=i, column=0, sticky="ew", pady=1)
            row.columnconfigure(1, weight=1)

            # Armed indicator
            armed_lbl = tk.Label(row,
                text="▶" if is_armed else ("◆" if is_pinned else "○"),
                font=("Courier", 8, "bold"), bg=row_bg,
                fg=ACCENT2 if is_armed else (SUCCESS if is_pinned else TEXT_DIM),
                padx=4)
            armed_lbl.grid(row=0, column=0, sticky="w")

            # SSID + coords
            coord_str = f"  ({int(pos[0])}, {int(pos[1])})" if pos else "  not pinned"
            name_lbl = tk.Label(row,
                text=f"{ssid[:14]}{coord_str}",
                font=("Courier", 7), bg=row_bg,
                fg=TEXT if is_pinned else TEXT_DIM,
                anchor="w", padx=2)
            name_lbl.grid(row=0, column=1, sticky="ew")

            # Remove button (only shown when pinned)
            if is_pinned:
                rm_btn = tk.Button(row, text="✕",
                    font=("Courier", 7), bg=row_bg, fg=DANGER,
                    relief="flat", padx=2, cursor="hand2",
                    command=lambda b=bssid: self._remove_ap_pin(b))
                rm_btn.grid(row=0, column=2, sticky="e", padx=2)

            # Click anywhere on the row to arm this BSSID
            for widget in (row, armed_lbl, name_lbl):
                widget.bind("<Button-1>",
                    lambda e, b=bssid: self._select_ap_for_pin(b))

    def _select_ap_for_pin(self, bssid: str):
        """Arm a BSSID for placement — next canvas click drops its pin there."""
        self._dragging_bssid = bssid
        if not self._ap_drag_mode:
            self._ap_drag_mode = True
            self.place_ap_btn.config(bg=ACCENT2, fg='black',
                                     text="◆  Placing APs  (click to cancel)")
        ssid = self.session.get_ssid(bssid)
        self._set_status(
            f"Click the location of  {ssid}  on the map.  "
            "Right-click an existing ◆ to remove it.")
        self.canvas_widget.get_tk_widget().config(cursor="crosshair")
        self._update_ap_positions_panel(highlight=bssid)

    def _remove_ap_pin(self, bssid: str):
        """Remove a single AP pin."""
        if bssid in self.session.ap_positions:
            self.session.ap_positions.pop(bssid)
            if self._dragging_bssid == bssid:
                self._dragging_bssid = None
            ssid = self.session.get_ssid(bssid)
            self._set_status(f"◆ Removed pin for {ssid}")
            self._update_ap_positions_panel()
            self._redraw()

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

    # ── AP position drag-and-drop ──────────────────────────────────────────────

    def _toggle_ap_drag_mode(self):
        """Toggle AP placement mode on/off."""
        self._ap_drag_mode = not self._ap_drag_mode
        if self._ap_drag_mode:
            self.place_ap_btn.config(bg=ACCENT2, fg='black')
            self._set_status(
                "◆ Place APs mode — click an AP in the list, then click its location on the map. "
                "Right-click an existing pin to remove it.")
            self.canvas_widget.get_tk_widget().config(cursor="crosshair")
            # Highlight the active_bssid_listbox so user knows to click there first
            self.active_bssid_listbox.config(highlightthickness=2,
                                              highlightcolor=ACCENT2,
                                              highlightbackground=ACCENT2)
        else:
            self._ap_drag_mode = False
            self._dragging_bssid = None
            self.place_ap_btn.config(bg=BG2, fg=ACCENT2)
            self._set_status("Ready")
            self.canvas_widget.get_tk_widget().config(cursor="")
            self.active_bssid_listbox.config(highlightthickness=0)

    def _get_selected_listbox_bssid(self) -> Optional[str]:
        """Return the BSSID currently selected in the active networks listbox."""
        sel = self.active_bssid_listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        bssids = self.selected_bssids_for_heatmap
        if idx < len(bssids):
            return bssids[idx]
        return None

    def _on_canvas_click(self, event):
        """Handles both placement-mode measurement recording and AP pin dropping."""
        if event.inaxes != self.ax: return
        x, y = event.xdata, event.ydata
        if x is None or y is None: return

        # ── Right-click: remove AP pin under cursor ────────────────────────────
        if event.button == 3 and self._ap_drag_mode:
            hit_radius = max(self.session.canvas_width,
                             self.session.canvas_height) * 0.025
            for bssid, (px, py) in list(self.session.ap_positions.items()):
                if abs(px - x) < hit_radius and abs(py - y) < hit_radius:
                    del self.session.ap_positions[bssid]
                    ssid = self.session.get_ssid(bssid)
                    self._set_status(f"Removed pin for {ssid}")
                    self._redraw()
                    return
            return

        # ── AP drag mode: left-click drops the selected AP ─────────────────────
        if self._ap_drag_mode and event.button == 1:
            bssid = self._dragging_bssid
            if not bssid:
                self._set_status("⚠ Click a network in the AP Positions panel first, then click its location.")
                return
            self.session.ap_positions[bssid] = (x, y)
            ssid = self.session.get_ssid(bssid)
            self._dragging_bssid = None
            self._set_status(f"◆ Pinned {ssid} at ({int(x)}, {int(y)})  —  right-click pin to remove")
            self._update_ap_positions_panel()
            self._redraw()
            return

        # ── Normal measurement placement ───────────────────────────────────────
        if not self._placement_mode or not self._pending_scan: return

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

    def _on_canvas_motion(self, event):
        """Show a ghost diamond following the cursor in AP drag mode."""
        if not self._ap_drag_mode: return
        if event.inaxes != self.ax: return
        if event.xdata is None or event.ydata is None: return

        bssid = self._dragging_bssid
        if not bssid: return

        # Draw a ghost diamond at cursor position — redraw is expensive so we
        # use blit-safe artists: remove the old ghost and add a new one.
        if self._drag_ghost:
            try:
                self._drag_ghost.remove()
            except Exception:
                pass
            self._drag_ghost = None

        import matplotlib.patches as mpatches
        import matplotlib.path as mpath

        w  = self.session.canvas_width
        h  = self.session.canvas_height
        r  = max(w, h) * 0.012 * 1.6
        px, py = event.xdata, event.ydata

        verts = [(px, py-r), (px+r, py), (px, py+r), (px-r, py), (px, py-r)]
        codes = [mpath.Path.MOVETO, mpath.Path.LINETO, mpath.Path.LINETO,
                 mpath.Path.LINETO, mpath.Path.CLOSEPOLY]
        self._drag_ghost = mpatches.PathPatch(
            mpath.Path(verts, codes),
            facecolor=ACCENT2, edgecolor='white',
            linewidth=1.5, zorder=10, alpha=0.6
        )
        self.ax.add_patch(self._drag_ghost)
        self.canvas_widget.draw_idle()

    def _on_canvas_release(self, event):
        """Clean up ghost on mouse release (no action needed — drop is on click)."""
        if self._drag_ghost:
            try:
                self._drag_ghost.remove()
            except Exception:
                pass
            self._drag_ghost = None
        if self._ap_drag_mode:
            self.canvas_widget.draw_idle()

    def _clear_ap_positions(self):
        """Remove all AP position pins from the session."""
        if not self.session.ap_positions: return
        if messagebox.askyesno("Clear AP Pins", "Remove all AP position pins?"):
            self.session.ap_positions.clear()
            self._set_status("All AP pins cleared")
            self._redraw()

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
        self._ap_drag_mode = False
        self._dragging_bssid = None
        self.place_ap_btn.config(bg=BG2, fg=ACCENT2)
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
        self._update_ap_positions_panel()
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
    parser = argparse.ArgumentParser(description="pyFi — WiFi Heatmap Generator")
    parser.add_argument("--session", help="Path to a .json session file to load on startup")
    args = parser.parse_args()

    root = tk.Tk()
    root.title("pyFi")

    # Set taskbar / window icon
    _icon_path = _resource_path(os.path.join("extras", "pyfi-logo.ico"))
    try:
        root.iconbitmap(default=_icon_path)
    except Exception:
        try:
            _icon_img = tk.PhotoImage(file=_icon_path)
            root.iconphoto(True, _icon_img)
        except Exception:
            pass   # icon is cosmetic — silently skip if unavailable

    WiFiHeatmapApp(root, initial_session=args.session)
    root.protocol("WM_DELETE_WINDOW", lambda: (root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
