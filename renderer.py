"""
renderer.py — Renders WiFi heatmaps using matplotlib.
Supports both floorplan overlay and grid-only mode.
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.figure import Figure
from matplotlib.axes import Axes
from matplotlib.patches import Circle
from typing import Optional
from PIL import Image

from data import Session, Measurement
from interpolator import interpolate, HeatmapData, get_signal_at


# Custom colormap: deep blue (weak) → cyan → green → yellow → red (strong)
SIGNAL_COLORS = [
    (0.05, 0.05, 0.35),   # very weak: dark navy
    (0.10, 0.40, 0.80),   # weak: blue
    (0.10, 0.80, 0.80),   # moderate: cyan
    (0.20, 0.90, 0.20),   # good: green
    (1.00, 0.90, 0.10),   # strong: yellow
    (1.00, 0.40, 0.00),   # very strong: orange
    (0.90, 0.05, 0.05),   # excellent: red
]
SIGNAL_CMAP = mcolors.LinearSegmentedColormap.from_list("wifi_signal", SIGNAL_COLORS)


def render_heatmap(
    session: Session,
    bssid: str,
    fig: Optional[Figure] = None,
    ax: Optional[Axes] = None,
    alpha: float = 0.65,
    show_points: bool = True,
    show_colorbar: bool = True,
    export_path: Optional[str] = None,
) -> tuple[Figure, Axes, Optional[HeatmapData]]:
    """
    Render a heatmap for a specific BSSID onto a matplotlib figure.

    Args:
        session:        The data session with measurements.
        bssid:          The BSSID to visualize.
        fig/ax:         Existing figure/axes to draw onto (creates new if None).
        alpha:          Transparency of the heatmap overlay.
        show_points:    Whether to draw measurement dot markers.
        show_colorbar:  Whether to draw the dBm color scale.
        export_path:    If set, saves figure to this path.

    Returns:
        (figure, axes, heatmap_data)
    """
    if fig is None or ax is None:
        fig, ax = plt.subplots(figsize=(10, 7))

    ax.clear()

    points, values = session.get_points_and_values(bssid)
    ssid = session.get_ssid(bssid)
    w, h = session.canvas_width, session.canvas_height

    # ── Draw background ───────────────────────────────────────────────────────
    if session.floorplan_path:
        try:
            img = Image.open(session.floorplan_path).convert("RGBA")
            img = img.resize((w, h), Image.LANCZOS)
            ax.imshow(np.array(img), extent=[0, w, h, 0], aspect='auto', zorder=1)
        except Exception as e:
            _draw_grid_background(ax, w, h)
            print(f"[renderer] Could not load floorplan: {e}")
    else:
        _draw_grid_background(ax, w, h)

    heatmap_data = None

    if len(points) >= 2:
        # ── Interpolate & draw heatmap ─────────────────────────────────────
        heatmap_data = interpolate(points, values, w, h)
        img_heatmap = ax.imshow(
            heatmap_data.grid_z,
            extent=[0, w, 0, h],
            origin='upper',
            cmap=SIGNAL_CMAP,
            vmin=-90,
            vmax=-30,
            alpha=alpha,
            zorder=2,
            aspect='auto'
        )

        if show_colorbar:
            cbar = fig.colorbar(img_heatmap, ax=ax, fraction=0.03, pad=0.02)
            cbar.set_label("Signal Strength (dBm)", fontsize=10)
            cbar.ax.tick_params(labelsize=8)
            # Add quality labels
            cbar.ax.text(1.4, -90, "Poor", transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='gray', va='bottom')
            cbar.ax.text(1.4, -30, "Excellent", transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='gray', va='top')
    else:
        ax.text(w / 2, h / 2,
                f"Not enough data for '{ssid}'\n({len(points)} point{'s' if len(points) != 1 else ''} collected — need at least 2)",
                ha='center', va='center', fontsize=13, color='#aaaaaa',
                bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a2e', alpha=0.7))

    # ── Draw measurement points ───────────────────────────────────────────────
    if show_points and points:
        for i, ((px, py), dbm) in enumerate(zip(points, values)):
            color = SIGNAL_CMAP((dbm + 90) / 60)
            circle = Circle((px, py), radius=max(w, h) * 0.012,
                            facecolor=color, edgecolor='white',
                            linewidth=1.5, zorder=5, alpha=0.95)
            ax.add_patch(circle)
            ax.text(px, py - max(w, h) * 0.022, f"{dbm}",
                    ha='center', va='bottom', fontsize=7, color='white',
                    fontweight='bold', zorder=6)

    # ── Labels & styling ──────────────────────────────────────────────────────
    ap_label = f"{ssid}  •  {bssid}"
    point_note = f"{len(points)} measurement{'s' if len(points) != 1 else ''}"
    if not session.floorplan_path:
        point_note += "  ⚠ No floorplan — adding one increases accuracy"

    ax.set_title(f"WiFi Heatmap\n{ap_label}", fontsize=12, fontweight='bold',
                 color='white', pad=10)
    ax.set_xlabel(point_note, fontsize=9, color='#888888')
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_facecolor('#0d0d1a')
    fig.patch.set_facecolor('#0d0d1a')

    if export_path:
        fig.savefig(export_path, dpi=150, bbox_inches='tight',
                    facecolor=fig.get_facecolor())

    return fig, ax, heatmap_data


def _draw_grid_background(ax: Axes, w: int, h: int):
    """Draw a subtle technical grid when no floorplan is available."""
    ax.set_facecolor('#0d0d1a')
    grid_spacing = max(w, h) // 10

    for x in range(0, w + 1, grid_spacing):
        ax.axvline(x, color='#1e2a4a', linewidth=0.6, zorder=0)
    for y in range(0, h + 1, grid_spacing):
        ax.axhline(y, color='#1e2a4a', linewidth=0.6, zorder=0)

    # Label grid coordinates lightly
    for x in range(0, w + 1, grid_spacing * 2):
        ax.text(x, h - 4, str(x), ha='center', va='bottom',
                fontsize=6, color='#2a3a5a')
    for y in range(0, h + 1, grid_spacing * 2):
        ax.text(2, y, str(y), ha='left', va='center',
                fontsize=6, color='#2a3a5a')
