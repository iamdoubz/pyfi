"""
interpolator.py — Spatial interpolation of WiFi signal data.
Turns sparse measurement points into a smooth heatmap grid.
"""

import numpy as np
from scipy.interpolate import griddata, RBFInterpolator
from dataclasses import dataclass
from typing import Optional


@dataclass
class HeatmapData:
    grid_z: np.ndarray          # 2D interpolated signal grid
    grid_x: np.ndarray          # X mesh
    grid_y: np.ndarray          # Y mesh
    width: int
    height: int
    vmin: float = -90.0
    vmax: float = -30.0
    point_count: int = 0


def interpolate(
    points: list[tuple[float, float]],
    values: list[float],
    width: int,
    height: int,
    resolution: int = 300,
    method: str = "auto"
) -> HeatmapData:
    """
    Interpolate sparse signal measurements into a dense grid.

    Args:
        points:     List of (x, y) measurement positions (pixel coords).
        values:     Signal strength (dBm) at each point.
        width:      Canvas/image width in pixels.
        height:     Canvas/image height in pixels.
        resolution: Grid density (higher = smoother but slower).
        method:     "linear", "cubic", "rbf", or "auto" (picks best for point count).

    Returns:
        HeatmapData with the interpolated 2D grid.
    """
    # Minimum points required per method:
    #   linear  needs 3+ non-collinear points (Qhull triangulation)
    #   cubic   needs 4+ non-collinear points
    #   rbf     needs 3+ points (no triangulation — always safe)
    # We use 4 as the universal safe floor since collinearity can still
    # trip up linear with exactly 3 points. Below 4, we use nearest-neighbor
    # which simply paints each pixel with the closest measured value.
    MIN_POINTS_FOR_INTERPOLATION = 4

    if len(points) < MIN_POINTS_FOR_INTERPOLATION:
        raise ValueError(
            f"Need at least {MIN_POINTS_FOR_INTERPOLATION} measurement points to interpolate "
            f"(have {len(points)})."
        )

    pts = np.array(points, dtype=float)
    vals = np.array(values, dtype=float)

    # Auto-select method based on point count
    if method == "auto":
        if len(points) >= 9:
            method = "rbf"
        elif len(points) >= 4:
            method = "cubic"
        else:
            method = "linear"  # only reached if caller forces method="auto" is bypassed

    # Build output grid
    gx = np.linspace(0, width, resolution)
    gy = np.linspace(0, height, resolution)
    grid_x, grid_y = np.meshgrid(gx, gy)

    if method == "rbf":
        grid_z = _rbf_interpolate(pts, vals, grid_x, grid_y)
    else:
        grid_z = griddata(pts, vals, (grid_x, grid_y), method=method)
        # Fill NaN edges with nearest-neighbor fallback
        nan_mask = np.isnan(grid_z)
        if nan_mask.any():
            fallback = griddata(pts, vals, (grid_x, grid_y), method="nearest")
            grid_z[nan_mask] = fallback[nan_mask]

    return HeatmapData(
        grid_z=grid_z,
        grid_x=grid_x,
        grid_y=grid_y,
        width=width,
        height=height,
        vmin=float(np.min(vals)) - 5,
        vmax=float(np.max(vals)) + 5,
        point_count=len(points)
    )


def _rbf_interpolate(
    pts: np.ndarray,
    vals: np.ndarray,
    grid_x: np.ndarray,
    grid_y: np.ndarray
) -> np.ndarray:
    """
    Radial Basis Function interpolation — smoother results for denser datasets.
    Uses a thin-plate spline kernel which works well for signal propagation.
    """
    # Normalize coordinates for numerical stability
    scale_x = grid_x.max()
    scale_y = grid_y.max()
    pts_norm = pts / [scale_x, scale_y]
    query_norm = np.column_stack([
        grid_x.ravel() / scale_x,
        grid_y.ravel() / scale_y
    ])

    rbf = RBFInterpolator(pts_norm, vals, kernel="thin_plate_spline", smoothing=1.0)
    result = rbf(query_norm)
    return result.reshape(grid_x.shape)


def get_signal_at(
    x: float, y: float,
    heatmap: HeatmapData
) -> Optional[float]:
    """Look up interpolated signal strength at a given pixel coordinate."""
    if heatmap.grid_z is None:
        return None

    # Map pixel coords to grid indices
    h, w = heatmap.grid_z.shape
    ix = int(x / heatmap.width * w)
    iy = int(y / heatmap.height * h)
    ix = max(0, min(w - 1, ix))
    iy = max(0, min(h - 1, iy))

    val = heatmap.grid_z[iy, ix]
    return float(val) if not np.isnan(val) else None
