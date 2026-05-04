"""
renderer.py — Renders WiFi heatmaps using matplotlib.
Supports both floorplan overlay and grid-only mode.

Ekahau-style rendering:
  - Colormap runs red (strongest) → orange → yellow → green → teal → blue →
    violet (weakest), matching professional site survey tools.
  - Alpha channel is derived per-pixel from signal strength so strong-signal
    zones are opaque and weak/absent zones fade to transparent, revealing the
    floorplan or grid beneath. This produces the "bubble" zone appearance.
  - A Gaussian smoothing pass is applied to the RGBA image to soften the
    zone edges and blend overlapping coverage areas naturally.
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.figure import Figure
from matplotlib.axes import Axes
from matplotlib.patches import Circle
from scipy.ndimage import gaussian_filter
from typing import Optional
from PIL import Image

from data import Session
from interpolator import interpolate, HeatmapData


# ── Colormaps ─────────────────────────────────────────────────────────────────

# Ekahau-style: red (excellent) → orange → yellow → green → teal → blue → violet (poor)
# This is the reverse of our old colormap — strong signal is warm, weak is cool.
_EKAHAU_COLORS = [
    (0.45, 0.00, 0.55),   # -90 dBm  violet / purple
    (0.10, 0.10, 0.80),   # -80 dBm  blue
    (0.00, 0.60, 0.80),   # -70 dBm  teal
    (0.10, 0.80, 0.30),   # -65 dBm  green
    (0.70, 0.95, 0.10),   # -58 dBm  yellow-green
    (1.00, 0.85, 0.00),   # -52 dBm  yellow
    (1.00, 0.50, 0.00),   # -46 dBm  orange
    (0.95, 0.10, 0.10),   # -30 dBm  red
]
SIGNAL_CMAP = mcolors.LinearSegmentedColormap.from_list("ekahau", _EKAHAU_COLORS)

# Separate single-color black cmap used only for the "no signal" dot markers
_BLACK_CMAP = mcolors.LinearSegmentedColormap.from_list("black_only",
                                                         [(0,0,0), (0,0,0)])

# dBm range the colormap spans
VMIN, VMAX = -90, -30


def render_heatmap(
    session: Session,
    bssids: list[str],
    fig: Optional[Figure] = None,
    ax: Optional[Axes] = None,
    alpha: float = 0.85,
    show_points: bool = True,
    show_colorbar: bool = True,
    export_path: Optional[str] = None,
) -> tuple[Figure, Axes, Optional[HeatmapData]]:
    """
    Render an Ekahau-style heatmap for one or more BSSIDs.

    The heatmap is composited as an RGBA image where:
      - RGB  = signal strength mapped through the Ekahau colormap
      - Alpha = derived from signal strength so strong zones are opaque and
                weak/absent zones fade to transparent, revealing the background.

    If any selected BSSIDs have physical positions set in session.ap_positions,
    synthetic anchor points are injected into the interpolation so the heatmap
    peak is correctly located at the real transmitter position.  AP positions
    are shown as distinct diamond icons, visually separate from measurement dots.

    Args:
        session:        The data session with measurements.
        bssids:         List of BSSIDs. Multiple BSSIDs are averaged per point.
        fig/ax:         Existing figure/axes (creates new if None).
        alpha:          Maximum opacity of the heatmap overlay (default 0.85).
        show_points:    Draw measurement dot markers and AP position icons.
        show_colorbar:  Draw the dBm color scale.
        export_path:    If set, save figure to this path.

    Returns:
        (figure, axes, heatmap_data)
    """
    if not bssids:
        raise ValueError("At least one BSSID must be provided.")

    if fig is None or ax is None:
        fig, ax = plt.subplots(figsize=(10, 7))
        fig.subplots_adjust(right=0.88)

    # Remove stale colorbar axes from previous renders
    for ax_obj in fig.axes[1:]:
        ax_obj.remove()
    ax.clear()

    points, values   = session.get_points_and_values_multi(bssids)
    missing_points   = session.get_missing_points(bssids)
    anchor_pts, anchor_vals = session.get_anchor_points(bssids)
    w, h             = session.canvas_width, session.canvas_height

    # Merge anchor points into the interpolation input when available.
    # Anchors are synthetic — they don't appear as measurement dots.
    interp_points = points + anchor_pts
    interp_values = values + anchor_vals

    # ── Title label ───────────────────────────────────────────────────────────
    if len(bssids) == 1:
        ssid     = session.get_ssid(bssids[0])
        ap_label = f"{ssid}  •  {bssids[0]}"
    else:
        ssids    = list({session.get_ssid(b) for b in bssids})
        ssid     = ssids[0] if len(ssids) == 1 else f"{len(ssids)} networks"
        ap_label = f"{ssid}  •  {len(bssids)} BSSIDs averaged"

    # ── Background ────────────────────────────────────────────────────────────
    ax.set_facecolor('#1a1a1a')
    fig.patch.set_facecolor('#0d0d1a')

    if session.floorplan_path:
        try:
            fp_img = Image.open(session.floorplan_path).convert("RGBA")
            fp_img = fp_img.resize((w, h), Image.LANCZOS)
            ax.imshow(np.array(fp_img), extent=[0, w, h, 0],
                      aspect='auto', zorder=1)
        except Exception as e:
            _draw_grid_background(ax, w, h)
            print(f"[renderer] Could not load floorplan: {e}")
    else:
        _draw_grid_background(ax, w, h)

    # ── Heatmap ───────────────────────────────────────────────────────────────
    heatmap_data = None
    MIN_POINTS   = 4

    if len(interp_points) >= MIN_POINTS:
        heatmap_data = interpolate(interp_points, interp_values, w, h)
        grid         = heatmap_data.grid_z

        rgba = _build_ekahau_rgba(grid, alpha)

        # Use a standalone ScalarMappable to drive the colorbar.
        # The hidden alpha=0 imshow blanks the colorbar on many backends
        # because matplotlib treats a fully-transparent mappable as having
        # no drawable content. A detached ScalarMappable with norm and cmap
        # set explicitly is always rendered correctly.
        _sm = plt.cm.ScalarMappable(
            cmap=SIGNAL_CMAP,
            norm=mcolors.Normalize(vmin=VMIN, vmax=VMAX))
        _sm.set_array([])   # required by matplotlib even when supplying no data

        ax.imshow(
            rgba,
            extent=[0, w, 0, h], origin='upper',
            aspect='auto', zorder=2, interpolation='bilinear'
        )

        if show_colorbar:
            cax  = fig.add_axes([0.91, 0.15, 0.02, 0.70])
            cbar = fig.colorbar(_sm, cax=cax)
            cbar.set_label("Signal Strength (dBm)", fontsize=10)
            cbar.ax.tick_params(labelsize=8)
            cbar.ax.text(1.4, VMIN, "Poor",
                         transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='gray', va='bottom')
            cbar.ax.text(1.4, VMAX, "Excellent",
                         transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='gray', va='top')
            cbar.ax.text(1.4, VMIN - 5, "No signal",
                         transform=cbar.ax.get_yaxis_transform(),
                         fontsize=7, color='black', va='top',
                         bbox=dict(facecolor='white', edgecolor='none', pad=1.5))

    else:
        collected = len(interp_points)
        remaining = MIN_POINTS - collected
        bar = f"[{'█' * collected}{'░' * remaining}]  {collected}/{MIN_POINTS}"
        msg = (f"Collecting data\n\n{bar}\n\n"
               f"Add {remaining} more point{'s' if remaining != 1 else ''} "
               f"to generate the heatmap.")
        ax.text(w / 2, h / 2, msg,
                ha='center', va='center', fontsize=12,
                color='#8888bb', linespacing=1.9, fontfamily='monospace',
                bbox=dict(boxstyle='round,pad=0.8',
                          facecolor='#10101e', alpha=0.8),
                zorder=3)

    # ── Measurement dots ──────────────────────────────────────────────────────
    radius = max(w, h) * 0.012
    if show_points:
        # Valid signal measurement points — filled circles
        for (px, py), dbm in zip(points, values):
            norm_val = (dbm - VMIN) / (VMAX - VMIN)
            color    = SIGNAL_CMAP(norm_val)
            ax.add_patch(Circle((px, py), radius=radius,
                                facecolor=color, edgecolor='white',
                                linewidth=1.5, zorder=5, alpha=0.95))
            ax.text(px, py - radius * 1.8, f"{int(round(dbm))}",
                    ha='center', va='bottom', fontsize=7,
                    color='white', fontweight='bold', zorder=6)

        # Out-of-range measurement points — black circles with "?"
        for (px, py) in missing_points:
            ax.add_patch(Circle((px, py), radius=radius,
                                facecolor='black', edgecolor='white',
                                linewidth=1.5, zorder=5, alpha=0.95))
            ax.text(px, py, "?",
                    ha='center', va='center', fontsize=7,
                    color='white', fontweight='bold', zorder=6)

        # AP position markers — drawn last so they sit on top of everything
        _draw_ap_markers(ax, session, bssids, radius)

    # ── Labels & axes ─────────────────────────────────────────────────────────
    total      = len(points) + len(missing_points)
    point_note = f"{total} measurement{'s' if total != 1 else ''}"
    if missing_points:
        point_note += f"  •  {len(missing_points)} out of range (●)"
    if len(bssids) > 1:
        point_note += f"  •  avg of {len(bssids)} BSSIDs"
    if anchor_pts:
        point_note += f"  •  {len(anchor_pts)} AP position{'s' if len(anchor_pts) != 1 else ''} pinned (◆)"
    if not session.floorplan_path:
        point_note += "  ⚠ No floorplan — adding one increases accuracy"

    ax.set_title(f"WiFi Heatmap\n{ap_label}",
                 fontsize=12, fontweight='bold', color='white', pad=10)
    ax.set_xlabel(point_note, fontsize=9, color='#888888')
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.set_xticks([])
    ax.set_yticks([])

    if export_path:
        fig.savefig(export_path, dpi=150, bbox_inches='tight',
                    facecolor=fig.get_facecolor())

    return fig, ax, heatmap_data


# ── RGBA builder ──────────────────────────────────────────────────────────────

def _build_ekahau_rgba(grid: np.ndarray, max_alpha: float) -> np.ndarray:
    """
    Convert a dBm grid into an RGBA uint8 image with Ekahau-style rendering.

    Alpha channel logic:
      - Signal ≥ VMAX (-30 dBm)  → fully opaque (max_alpha)
      - Signal ≤ FADE_FLOOR      → fully transparent
      - Between                  → smooth cosine ramp so zone edges blend
                                   naturally without a hard cutoff

    A Gaussian blur is applied to the alpha channel before compositing so
    overlapping coverage zones blend smoothly rather than showing sharp
    interpolation triangle edges.
    """
    FADE_FLOOR = -85.0    # dBm at which the overlay becomes fully transparent
    BLUR_SIGMA = 8.0      # pixels — controls softness of zone edges

    res_h, res_w = grid.shape

    # ── RGB from colormap ─────────────────────────────────────────────────────
    norm      = np.clip((grid - VMIN) / (VMAX - VMIN), 0.0, 1.0)
    rgb_float = SIGNAL_CMAP(norm)[:, :, :3]          # (H, W, 3) float 0-1

    # ── Alpha from signal strength ────────────────────────────────────────────
    # Use a cosine ramp so the transition from opaque to transparent is smooth
    # rather than linear (cosine gives a more natural "bubble" appearance).
    fade_range = VMAX - FADE_FLOOR
    t          = np.clip((grid - FADE_FLOOR) / fade_range, 0.0, 1.0)
    alpha_raw  = 0.5 * (1.0 - np.cos(np.pi * t))    # cosine ease-in-out

    # NaN cells (outside interpolation hull) → transparent
    alpha_raw[np.isnan(grid)] = 0.0

    # Gaussian blur softens zone boundaries and blends overlapping AP zones
    alpha_blur = gaussian_filter(alpha_raw, sigma=BLUR_SIGMA)
    alpha_blur = np.clip(alpha_blur * max_alpha, 0.0, max_alpha)

    # ── Assemble RGBA uint8 ───────────────────────────────────────────────────
    rgba          = np.zeros((res_h, res_w, 4), dtype=np.uint8)
    rgba[:, :, 0] = (rgb_float[:, :, 0] * 255).astype(np.uint8)
    rgba[:, :, 1] = (rgb_float[:, :, 1] * 255).astype(np.uint8)
    rgba[:, :, 2] = (rgb_float[:, :, 2] * 255).astype(np.uint8)
    rgba[:, :, 3] = (alpha_blur          * 255).astype(np.uint8)

    return rgba


# ── AP position markers ───────────────────────────────────────────────────────

def _draw_ap_markers(ax: Axes, session: Session, bssids: list[str], radius: float):
    """
    Draw a distinct diamond icon at each AP's known physical position.

    Visual design (clearly different from measurement dots):
      - Outer diamond: white filled, black border — highly visible on any background
      - Inner WiFi arc symbol: drawn in the AP's signal color
      - SSID label below the icon
      - Small "◆" glyph instead of a dBm number so it's never confused with
        a measurement dot

    Only BSSIDs in `bssids` that also have an entry in session.ap_positions
    are drawn.
    """
    import matplotlib.patches as mpatches
    import matplotlib.path as mpath

    icon_r  = radius * 1.6    # diamond half-size — larger than measurement dots
    label_r = radius * 2.8    # label offset below center

    for bssid in bssids:
        if bssid not in session.ap_positions:
            continue
        px, py = session.ap_positions[bssid]
        ssid   = session.get_ssid(bssid)

        # ── Diamond shape (rotated square) via Path ───────────────────────────
        diamond_verts = [
            (px,          py - icon_r),   # top
            (px + icon_r, py           ),  # right
            (px,          py + icon_r),   # bottom
            (px - icon_r, py           ),  # left
            (px,          py - icon_r),   # close
        ]
        diamond_codes = [
            mpath.Path.MOVETO,
            mpath.Path.LINETO,
            mpath.Path.LINETO,
            mpath.Path.LINETO,
            mpath.Path.CLOSEPOLY,
        ]
        diamond_path  = mpath.Path(diamond_verts, diamond_codes)
        diamond_patch = mpatches.PathPatch(
            diamond_path,
            facecolor='white', edgecolor='#111111',
            linewidth=2.0, zorder=8, alpha=0.95
        )
        ax.add_patch(diamond_patch)

        # ── WiFi arc symbol (three concentric arcs, bottom-left origin) ───────
        # Drawn as text using the Unicode WiFi / signal character
        ax.text(px, py, "📶",
                ha='center', va='center',
                fontsize=max(6, int(icon_r * 0.55)),
                zorder=9, alpha=0.85)

        # ── SSID label below the diamond ──────────────────────────────────────
        ax.text(px, py + label_r, ssid,
                ha='center', va='top', fontsize=7,
                color='white', fontweight='bold', zorder=9,
                bbox=dict(boxstyle='round,pad=0.2',
                          facecolor='#111111', edgecolor='none', alpha=0.7))


# ── AP position markers ───────────────────────────────────────────────────────

def _draw_ap_markers(ax: Axes, session: Session, bssids: list[str], radius: float):
    """
    Draw a distinct diamond ◆ icon at each AP whose physical position is known.

    Visual design (intentionally different from measurement circles):
      - Outer diamond: white-filled rotated square, larger than measurement dots
      - Inner diamond: signal-colored fill
      - WiFi arc lines above the diamond
      - SSID label beneath
      - Thin pulse ring to make them easy to spot on a busy floorplan

    These markers are purely informational — they are not measurement points
    and do not affect the interpolation directly (the anchor injection in
    render_heatmap() handles that separately).
    """
    import matplotlib.patches as mpatches
    import matplotlib.transforms as mtransforms

    for bssid in bssids:
        if bssid not in session.ap_positions:
            continue

        px, py   = session.ap_positions[bssid]
        ssid     = session.get_ssid(bssid)
        r        = radius * 1.6    # diamond is larger than measurement dots

        # ── Outer pulse ring ─────────────────────────────────────────────────
        ax.add_patch(Circle((px, py), radius=r * 1.9,
                            facecolor='none', edgecolor='white',
                            linewidth=0.8, zorder=7, alpha=0.35,
                            linestyle='--'))

        # ── White border diamond ──────────────────────────────────────────────
        diamond_outer = mpatches.RegularPolygon(
            (px, py), numVertices=4, radius=r * 1.15,
            orientation=np.pi / 4,          # rotate 45° → diamond orientation
            facecolor='white', edgecolor='white',
            linewidth=0, zorder=8, alpha=0.95)
        ax.add_patch(diamond_outer)

        # ── Coloured inner diamond ────────────────────────────────────────────
        # Use the anchor dBm value for the color
        real_pts, real_vals = session.get_points_and_values(bssid)
        anchor_dbm = min(-25, max(real_vals) + 5) if real_vals else -35
        norm_val   = np.clip((anchor_dbm - VMIN) / (VMAX - VMIN), 0.0, 1.0)
        color      = SIGNAL_CMAP(norm_val)

        diamond_inner = mpatches.RegularPolygon(
            (px, py), numVertices=4, radius=r * 0.80,
            orientation=np.pi / 4,
            facecolor=color, edgecolor='none',
            zorder=9, alpha=0.95)
        ax.add_patch(diamond_inner)

        # ── WiFi arcs above the diamond ───────────────────────────────────────
        # Three concentric arcs drawn as partial circles
        for arc_r, arc_alpha in [(r * 0.55, 0.9), (r * 0.9, 0.65), (r * 1.25, 0.40)]:
            arc = mpatches.Arc(
                (px, py - r * 0.15),          # slightly above centre
                width=arc_r * 2, height=arc_r * 2,
                angle=0, theta1=30, theta2=150,   # upper semicircle arc
                color='white', linewidth=1.4,
                zorder=10, alpha=arc_alpha)
            ax.add_patch(arc)

        # ── SSID label below the marker ───────────────────────────────────────
        ax.text(px, py + r * 2.2, ssid,
                ha='center', va='top', fontsize=7,
                color='white', fontweight='bold', zorder=10,
                bbox=dict(boxstyle='round,pad=0.3',
                          facecolor='#000000', alpha=0.55,
                          edgecolor='none'))

        # ── ◆ glyph as a tiny anchor indicator ───────────────────────────────
        ax.text(px, py, '◆',
                ha='center', va='center', fontsize=6,
                color='white', zorder=11, alpha=0.7)



def _draw_grid_background(ax: Axes, w: int, h: int):
    """Draw a subtle coordinate grid when no floorplan is loaded."""
    ax.set_facecolor('#1a1a1a')
    spacing = max(w, h) // 10

    for x in range(0, w + 1, spacing):
        ax.axvline(x, color='#2a2a2a', linewidth=0.6, zorder=0)
    for y in range(0, h + 1, spacing):
        ax.axhline(y, color='#2a2a2a', linewidth=0.6, zorder=0)

    for x in range(0, w + 1, spacing * 2):
        ax.text(x, h - 4, str(x), ha='center', va='bottom',
                fontsize=6, color='#3a3a3a')
    for y in range(0, h + 1, spacing * 2):
        ax.text(2, y, str(y), ha='left', va='center',
                fontsize=6, color='#3a3a3a')
