"""
scanner.py — Cross-platform WiFi access point scanner.
Supports Windows, Linux (nmcli/iwlist), and macOS (airport).
"""

import subprocess
import platform
import re
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AccessPoint:
    ssid: str
    bssid: str          # MAC address — unique per physical AP
    signal_dbm: int     # e.g. -55 dBm (closer to 0 = stronger)
    channel: int = 0
    band: str = ""      # "2.4GHz" or "5GHz"

    def signal_percent(self) -> int:
        """Convert dBm to approximate 0-100% for display."""
        # Clamp between -90 (worst) and -30 (best)
        clamped = max(-90, min(-30, self.signal_dbm))
        return int((clamped + 90) / 60 * 100)

    def quality_label(self) -> str:
        pct = self.signal_percent()
        if pct >= 75:
            return "Excellent"
        elif pct >= 50:
            return "Good"
        elif pct >= 25:
            return "Fair"
        else:
            return "Poor"


def scan(retries: int = 3) -> list[AccessPoint]:
    """Scan for nearby WiFi access points. Returns list of AccessPoint objects."""
    os_name = platform.system()
    last_err = None
    for attempt in range(retries):
        try:
            if os_name == "Windows":
                return _scan_windows()
            elif os_name == "Linux":
                return _scan_linux()
            elif os_name == "Darwin":
                return _scan_macos()
            else:
                raise OSError(f"Unsupported OS: {os_name}")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5)
    raise RuntimeError(f"WiFi scan failed after {retries} attempts: {last_err}")


def scan_averaged(samples: int = 5, delay: float = 0.3) -> list[AccessPoint]:
    """
    Scan multiple times and average signal strengths per BSSID.
    More accurate than a single scan — recommended for data collection.
    """
    all_results: dict[str, list[int]] = {}  # bssid -> list of dBm readings
    ap_info: dict[str, AccessPoint] = {}

    for _ in range(samples):
        try:
            aps = scan()
            for ap in aps:
                key = ap.bssid.upper()
                if key not in all_results:
                    all_results[key] = []
                    ap_info[key] = ap
                all_results[key].append(ap.signal_dbm)
        except Exception:
            pass
        if _ < samples - 1:
            time.sleep(delay)

    averaged = []
    for bssid, readings in all_results.items():
        ap = ap_info[bssid]
        avg_dbm = int(sum(readings) / len(readings))
        averaged.append(AccessPoint(
            ssid=ap.ssid,
            bssid=ap.bssid,
            signal_dbm=avg_dbm,
            channel=ap.channel,
            band=ap.band
        ))

    return sorted(averaged, key=lambda a: a.signal_dbm, reverse=True)


# ── Windows ──────────────────────────────────────────────────────────────────

def _scan_windows() -> list[AccessPoint]:
    result = subprocess.run(
        ["netsh", "wlan", "show", "networks", "mode=bssid"],
        capture_output=True, text=True, timeout=15
    )
    output = result.stdout
    aps = []

    # Split by SSID blocks
    blocks = re.split(r'\nSSID \d+ :', output)
    for block in blocks[1:]:  # skip header
        ssid_match = re.search(r'^\s*(.+)', block)
        bssid_match = re.search(r'BSSID \d+\s*:\s*([0-9a-fA-F:]{17})', block)
        signal_match = re.search(r'Signal\s*:\s*(\d+)%', block)
        channel_match = re.search(r'Channel\s*:\s*(\d+)', block)

        if not (ssid_match and bssid_match and signal_match):
            continue

        ssid = ssid_match.group(1).strip()
        bssid = bssid_match.group(1).upper()
        signal_pct = int(signal_match.group(1))
        signal_dbm = (signal_pct // 2) - 100  # Windows % → dBm approximation
        channel = int(channel_match.group(1)) if channel_match else 0

        aps.append(AccessPoint(
            ssid=ssid,
            bssid=bssid,
            signal_dbm=signal_dbm,
            channel=channel,
            band="5GHz" if channel > 14 else "2.4GHz"
        ))

    return aps


# ── Linux ─────────────────────────────────────────────────────────────────────

def _scan_linux() -> list[AccessPoint]:
    """Try nmcli first, fall back to iwlist."""
    try:
        return _scan_linux_nmcli()
    except Exception:
        return _scan_linux_iwlist()


def _scan_linux_nmcli() -> list[AccessPoint]:
    result = subprocess.run(
        ["nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,CHAN,FREQ", "dev", "wifi", "list", "--rescan", "yes"],
        capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError("nmcli failed")

    aps = []
    for line in result.stdout.strip().splitlines():
        # nmcli -t separates with ':', backslash-escapes colons in values
        parts = re.split(r'(?<!\\):', line)
        if len(parts) < 4:
            continue

        ssid = parts[0].replace("\\:", ":").strip()
        bssid = parts[1].replace("\\:", ":").upper().strip()
        try:
            signal_pct = int(parts[2])
            signal_dbm = (signal_pct // 2) - 100
            channel = int(parts[3])
        except ValueError:
            continue

        freq = parts[4].strip() if len(parts) > 4 else ""
        band = "5GHz" if "5" in freq else "2.4GHz"

        aps.append(AccessPoint(
            ssid=ssid,
            bssid=bssid,
            signal_dbm=signal_dbm,
            channel=channel,
            band=band
        ))

    return aps


def _scan_linux_iwlist() -> list[AccessPoint]:
    import shutil
    iface = _get_linux_interface()
    tool = "sudo" if shutil.which("sudo") else ""
    cmd = [c for c in [tool, "iwlist", iface, "scan"] if c]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)

    aps = []
    cells = re.split(r'Cell \d+ -', result.stdout)
    for cell in cells[1:]:
        ssid_m = re.search(r'ESSID:"([^"]*)"', cell)
        bssid_m = re.search(r'Address:\s*([0-9A-Fa-f:]{17})', cell)
        signal_m = re.search(r'Signal level=(-?\d+)\s*dBm', cell)
        channel_m = re.search(r'Channel:(\d+)', cell)

        if not (ssid_m and bssid_m and signal_m):
            continue

        channel = int(channel_m.group(1)) if channel_m else 0
        aps.append(AccessPoint(
            ssid=ssid_m.group(1),
            bssid=bssid_m.group(1).upper(),
            signal_dbm=int(signal_m.group(1)),
            channel=channel,
            band="5GHz" if channel > 14 else "2.4GHz"
        ))

    return aps


def _get_linux_interface() -> str:
    """Find the first wireless interface."""
    result = subprocess.run(["iwconfig"], capture_output=True, text=True)
    match = re.search(r'^(\w+)\s+IEEE', result.stdout, re.MULTILINE)
    if match:
        return match.group(1)
    # Fallback: look in /proc/net/wireless
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                m = re.match(r'\s*(\w+):', line)
                if m:
                    return m.group(1)
    except FileNotFoundError:
        pass
    return "wlan0"


# ── macOS ─────────────────────────────────────────────────────────────────────

def _scan_macos() -> list[AccessPoint]:
    airport = (
        "/System/Library/PrivateFrameworks/Apple80211.framework"
        "/Versions/Current/Resources/airport"
    )
    result = subprocess.run(
        [airport, "-s"],
        capture_output=True, text=True, timeout=15
    )

    aps = []
    lines = result.stdout.strip().splitlines()
    for line in lines[1:]:  # skip header
        # Format: SSID  BSSID  RSSI  CHANNEL  HT  CC  SECURITY
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            # BSSID is always a MAC address pattern
            bssid_idx = next(i for i, p in enumerate(parts)
                             if re.match(r'[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:', p))
        except StopIteration:
            continue

        ssid = " ".join(parts[:bssid_idx])
        bssid = parts[bssid_idx].upper()
        try:
            signal_dbm = int(parts[bssid_idx + 1])
            channel_str = parts[bssid_idx + 2]
            channel = int(re.match(r'\d+', channel_str).group())
        except (ValueError, IndexError, AttributeError):
            continue

        aps.append(AccessPoint(
            ssid=ssid,
            bssid=bssid,
            signal_dbm=signal_dbm,
            channel=channel,
            band="5GHz" if channel > 14 else "2.4GHz"
        ))

    return aps
