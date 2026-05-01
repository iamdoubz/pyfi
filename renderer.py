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


# Custom colormap: black (no signal) → dark navy → blue → cyan → green → yellow → red (strong)
# Black occupies the very bottom so "not found" markers read naturally against the scale.
SIGNAL_COLORS = [
    (0.00, 0.00, 0.00),   # no signal: black  ← new
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
    bssids: list[str],
    fig: Optional[Figure] = None,
    ax: Optional[Axes] = None,
    alpha: float = 0.65,
    show_points: bool = True,
    show_colorbar: bool = True,
    export_path: Optional[str] = None,
) -> tuple[Figure, Axes, Optional[HeatmapData]]:
    """
    Render a heatmap for one or more BSSIDs onto a matplotlib figure.

    Args:
        session:        The data session with measurements.
        bssids:         List of BSSIDs to visualize. If more than one, signal
                        values are averaged per measurement point (only BSSIDs
                        actually visible at each point contribute to the average).
        fig/ax:         Existing figure/axes to draw onto (creates new if None).
        alpha:          Transparency of the heatmap overlay.
        show_points:    Whether to draw measurement dot markers.
        show_colorbar:  Whether to draw the dBm color scale.
        export_path:    If set, saves figure to this path.

    Returns:
        (figure, axes, heatmap_data)
    """
    if not bssids:
        raise ValueError("At least one BSSID must be provided.")

    if fig is None or ax is None:
        fig, ax = plt.subplots(figsize=(10, 7))

    # Remove any colorbars left over from previous renders.
    # Colorbars attach to the figure, not the axes, so ax.clear() doesn't touch them.
    for ax_obj in fig.axes[1:]:   # axes[0] is the main plot; everything else is a colorbar
        ax_obj.remove()
    ax.clear()

    points, values = session.get_points_and_values_multi(bssids)
    missing_points = session.get_missing_points(bssids)   # positions with no signal
    w, h = session.canvas_width, session.canvas_height

    # Build a human-readable label for the title
    if len(bssids) == 1:
        ssid = session.get_ssid(bssids[0])
        ap_label = f"{ssid}  •  {bssids[0]}"
    else:
        # Show the shared SSID if all BSSIDs share one, otherwise list count
        ssids = list({session.get_ssid(b) for b in bssids})
        ssid  = ssids[0] if len(ssids) == 1 else f"{len(ssids)} networks"
        ap_label = f"{ssid}  •  {len(bssids)} BSSIDs averaged"

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
    MIN_POINTS = 4  # must match interpolator.MIN_POINTS_FOR_INTERPOLATION

    if len(points) >= MIN_POINTS:
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
            cbar.ax.text(1.4, -90, "Poor", transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='gray', va='bottom')
            cbar.ax.text(1.4, -30, "Excellent", transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='gray', va='top')
            # Black swatch at the very bottom representing "no signal found"
            cbar.ax.text(1.4, -95, "No signal", transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='black', va='top',
                         bbox=dict(facecolor='white', edgecolor='none', pad=1.5))
    else:
        # ── Not enough points yet — show progress indicator ────────────────
        collected = len(points)
        remaining = MIN_POINTS - collected
        filled = "█" * collected
        empty  = "░" * remaining
        bar    = f"[{filled}{empty}]  {collected}/{MIN_POINTS}"

        msg = (
            f"Collecting data\n\n"
            f"{bar}\n\n"
            f"Add {remaining} more point{'s' if remaining != 1 else ''} to generate the heatmap.\n"
            f"Measurement dots are shown below."
        )
        ax.text(w / 2, h / 2, msg,
                ha='center', va='center', fontsize=12,
                color='#8888bb', linespacing=1.9,
                fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='#10101e', alpha=0.8),
                zorder=3)

    # ── Draw measurement points ───────────────────────────────────────────────
    radius = max(w, h) * 0.012
    if show_points:
        # Valid signal points — colored by strength
        for (px, py), dbm in zip(points, values):
            color = SIGNAL_CMAP((dbm + 90) / 60)
            ax.add_patch(Circle((px, py), radius=radius,
                                facecolor=color, edgecolor='white',
                                linewidth=1.5, zorder=5, alpha=0.95))
            ax.text(px, py - radius * 1.8, f"{int(round(dbm))}",
                    ha='center', va='bottom', fontsize=7, color='white',
                    fontweight='bold', zorder=6)

        # Missing signal points — black circle with "?" label
        for (px, py) in missing_points:
            ax.add_patch(Circle((px, py), radius=radius,
                                facecolor='black', edgecolor='white',
                                linewidth=1.5, zorder=5, alpha=0.95))
            ax.text(px, py, "?",
                    ha='center', va='center', fontsize=7, color='white',
                    fontweight='bold', zorder=6)

    # ── Labels & styling ──────────────────────────────────────────────────────
    total_points = len(points) + len(missing_points)
    point_note = f"{total_points} measurement{'s' if total_points != 1 else ''}"
    if missing_points:
        point_note += f"  •  {len(missing_points)} out of range (●)"
    if len(bssids) > 1:
        point_note += f"  •  avg of {len(bssids)} BSSIDs"
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

    for x in range(0, w + 1, grid_spacing * 2):
        ax.text(x, h - 4, str(x), ha='center', va='bottom',
                fontsize=6, color='#2a3a5a')
    for y in range(0, h + 1, grid_spacing * 2):
        ax.text(2, y, str(y), ha='left', va='center',
                fontsize=6, color='#2a3a5a')
