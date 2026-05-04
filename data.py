"""
data.py — Session data model and JSON persistence.
Stores measurement points and metadata across sessions.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime


@dataclass
class Measurement:
    x: float                        # Pixel x on canvas
    y: float                        # Pixel y on canvas
    signals: dict[str, int]         # {BSSID: dBm}
    ssid_map: dict[str, str]        # {BSSID: SSID}
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class Session:
    name: str = "Untitled Session"
    floorplan_path: Optional[str] = None   # None = no floorplan (grid used instead)
    canvas_width: int = 800
    canvas_height: int = 600
    measurements: list[Measurement] = field(default_factory=list)
    # Physical positions of access points on the canvas, keyed by BSSID.
    # Optional — when set, the renderer uses these as anchor points to pin the
    # interpolation peak to the real transmitter location.
    ap_positions: dict[str, tuple[float, float]] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now

    def add_measurement(self, m: Measurement):
        self.measurements.append(m)
        self.updated_at = datetime.now().isoformat()

    def remove_measurement(self, index: int):
        if 0 <= index < len(self.measurements):
            self.measurements.pop(index)
            self.updated_at = datetime.now().isoformat()

    def get_all_bssids(self) -> list[str]:
        """Return sorted list of all unique BSSIDs seen across all measurements."""
        bssids = set()
        for m in self.measurements:
            bssids.update(m.signals.keys())
        return sorted(bssids)

    def get_ssid(self, bssid: str) -> str:
        """Look up SSID for a BSSID across all measurements."""
        for m in self.measurements:
            if bssid in m.ssid_map:
                return m.ssid_map[bssid]
        return bssid  # fallback to BSSID

    def get_points_and_values(self, bssid: str) -> tuple[list, list]:
        """Extract (x,y) points and dBm values for a specific BSSID."""
        points, values = [], []
        for m in self.measurements:
            if bssid in m.signals:
                points.append((m.x, m.y))
                values.append(m.signals[bssid])
        return points, values

    def get_points_and_values_multi(self, bssids: list[str]) -> tuple[list, list]:
        """
        Extract (x,y) points and averaged dBm values across multiple BSSIDs.

        For each measurement point, only the BSSIDs that were actually visible
        at that location are included in the average — a BSSID that was out of
        range at a particular point is excluded rather than dragging the average
        down with a floor value. Points where none of the requested BSSIDs were
        seen at all are skipped entirely.

        When a single BSSID is passed this is identical to get_points_and_values().
        """
        if len(bssids) == 1:
            return self.get_points_and_values(bssids[0])

        points, values = [], []
        for m in self.measurements:
            readings = [m.signals[b] for b in bssids if b in m.signals]
            if readings:
                points.append((m.x, m.y))
                values.append(sum(readings) / len(readings))
        return points, values

    def get_missing_points(self, bssids: list[str]) -> list[tuple[float, float]]:
        """
        Return positions of measurements where none of the requested BSSIDs
        were visible. These are locations where the user took a reading but
        the selected network(s) were completely out of range.
        """
        missing = []
        for m in self.measurements:
            if not any(b in m.signals for b in bssids):
                missing.append((m.x, m.y))
        return missing

    def get_anchor_points(self, bssids: list[str]) -> tuple[list, list]:
        """
        Return synthetic (x, y) anchor points and estimated dBm values for any
        BSSID in `bssids` that has a known physical position in ap_positions.

        The anchor value is the strongest real measurement seen for that BSSID
        plus 5 dBm (capped at -25 dBm), representing the expected near-field
        signal directly at the transmitter.  These points are injected into the
        interpolation so the heatmap peak is correctly anchored to the AP's
        physical location rather than estimated from surrounding measurements.
        """
        points, values = [], []
        for bssid in bssids:
            if bssid not in self.ap_positions:
                continue
            pos = self.ap_positions[bssid]
            # Find the strongest real reading for this BSSID
            real_pts, real_vals = self.get_points_and_values(bssid)
            if real_vals:
                anchor_dbm = min(-25, max(real_vals) + 5)
            else:
                anchor_dbm = -35   # reasonable default when no measurements exist yet
            points.append(pos)
            values.append(float(anchor_dbm))
        return points, values

    def save(self, path: str):
        """Save session to JSON file."""
        data = {
            "name": self.name,
            "floorplan_path": self.floorplan_path,
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "created_at": self.created_at,
            "updated_at": datetime.now().isoformat(),
            # Serialize ap_positions as {bssid: [x, y]} for JSON compatibility
            "ap_positions": {b: list(pos) for b, pos in self.ap_positions.items()},
            "measurements": [
                {
                    "x": m.x,
                    "y": m.y,
                    "signals": m.signals,
                    "ssid_map": m.ssid_map,
                    "timestamp": m.timestamp
                }
                for m in self.measurements
            ]
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str) -> "Session":
        """Load session from JSON file."""
        with open(path) as f:
            data = json.load(f)

        session = cls(
            name=data.get("name", "Loaded Session"),
            floorplan_path=data.get("floorplan_path"),
            canvas_width=data.get("canvas_width", 800),
            canvas_height=data.get("canvas_height", 600),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", "")
        )
        # Restore AP positions — stored as {bssid: [x, y]}, convert to tuples
        for bssid, pos in data.get("ap_positions", {}).items():
            session.ap_positions[bssid] = (float(pos[0]), float(pos[1]))
        for m in data.get("measurements", []):
            session.measurements.append(Measurement(
                x=m["x"],
                y=m["y"],
                signals=m["signals"],
                ssid_map=m.get("ssid_map", {}),
                timestamp=m.get("timestamp", "")
            ))
        return session
