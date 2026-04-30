"""
scanner.py — Cross-platform WiFi access point scanner.
Supports Windows (netsh), Linux (nmcli/iwlist), and macOS (airport).

Extended fields per AP: security, channel_width, mode (802.11 a/b/g/n/ac/ax),
frequency (MHz), and vendor (derived from BSSID OUI prefix).
"""

import subprocess
import platform
import re
import time
from dataclasses import dataclass
from typing import Optional


# ── OUI vendor table (top ~100 common WiFi chipset/router manufacturers) ──────
_OUI_TABLE: dict[str, str] = {
    # Cisco / Linksys
    "000C29": "Cisco",    "001A2F": "Cisco",    "00E0F7": "Cisco",
    "00173F": "Linksys",  "001346": "Linksys",
    # Netgear
    "001B2F": "Netgear",  "00224C": "Netgear",  "20E52A": "Netgear",
    "9C3DCF": "Netgear",  "A040A0": "Netgear",  "C03F0E": "Netgear",
    # TP-Link
    "B0487A": "TP-Link",  "C46E1F": "TP-Link",  "F4F26D": "TP-Link",
    "50C7BF": "TP-Link",  "8CAAB5": "TP-Link",  "D46E5C": "TP-Link",
    "A860B6": "TP-Link",  "1027F5": "TP-Link",  "30DE4B": "TP-Link",
    # ASUS
    "107B44": "ASUS",     "1C872C": "ASUS",     "2C56DC": "ASUS",
    "305A3A": "ASUS",     "382C4A": "ASUS",     "40167E": "ASUS",
    "50465D": "ASUS",     "6045CB": "ASUS",     "AC220B": "ASUS",
    # Apple
    "000A27": "Apple",    "000A95": "Apple",    "001124": "Apple",
    "001451": "Apple",    "001B63": "Apple",    "001CB3": "Apple",
    "001E52": "Apple",    "001FF3": "Apple",    "0021E9": "Apple",
    "0023DF": "Apple",    "002500": "Apple",    "3C0754": "Apple",
    "70CD60": "Apple",    "A45E60": "Apple",    "F0DBF8": "Apple",
    # Samsung
    "001632": "Samsung",  "002339": "Samsung",  "5425EA": "Samsung",
    "8C7712": "Samsung",  "A0821F": "Samsung",
    # D-Link
    "001195": "D-Link",   "0015E9": "D-Link",   "001CF0": "D-Link",
    "1C7EE5": "D-Link",   "280DFC": "D-Link",
    # Ubiquiti
    "002722": "Ubiquiti", "04186A": "Ubiquiti", "0418D6": "Ubiquiti",
    "24A43C": "Ubiquiti", "44D9E7": "Ubiquiti", "687278": "Ubiquiti",
    "788A20": "Ubiquiti", "802AA8": "Ubiquiti",
    # Aruba / HP
    "000B86": "Aruba",    "001A1E": "Aruba",    "20A6CD": "Aruba",
    "6C3B6B": "Aruba",    "94B4CF": "Aruba",
    # Google (Nest WiFi, OnHub)
    "F88FCA": "Google",   "1AC487": "Google",   "54607E": "Google",
    "A47733": "Google",   "3C5AB4": "Google",
    # Amazon (Eero, Echo)
    "F0272D": "Amazon",   "44650D": "Amazon",   "AC63BE": "Amazon",
    "34D270": "Amazon",
    # Belkin
    "001150": "Belkin",   "001CDF": "Belkin",   "0030BD": "Belkin",
    "94103E": "Belkin",   "EC1A59": "Belkin",
    # Huawei
    "001E10": "Huawei",   "002568": "Huawei",   "28311C": "Huawei",
    "3440B5": "Huawei",   "4C1FCC": "Huawei",   "6C8D37": "Huawei",
    "788C8A": "Huawei",   "AC853D": "Huawei",
    # Intel
    "0012F0": "Intel",    "001320": "Intel",    "0016EA": "Intel",
    "001E64": "Intel",    "002129": "Intel",
    # Qualcomm / Atheros
    "000F66": "Qualcomm", "001374": "Qualcomm",
    # Broadcom
    "000AF7": "Broadcom", "00904C": "Broadcom",
    # Ralink / MediaTek
    "000C43": "Ralink",   "00E04C": "Ralink",
    # MikroTik
    "4C5E0C": "MikroTik", "CC2DE0": "MikroTik", "E48D8C": "MikroTik",
    "B8690E": "MikroTik",
}


def lookup_vendor(bssid: str) -> str:
    """Return vendor name from BSSID OUI prefix, or '-' if unknown."""
    oui = bssid.upper().replace(":", "").replace("-", "")[:6]
    return _OUI_TABLE.get(oui, "-")


@dataclass
class AccessPoint:
    ssid: str
    bssid: str
    signal_dbm: int
    channel: int = 0
    band: str = ""
    freq_mhz: int = 0
    channel_width: str = ""
    security: str = ""
    mode: str = ""
    vendor: str = "-"

    def __post_init__(self):
        if not self.vendor or self.vendor == "-":
            self.vendor = lookup_vendor(self.bssid)
        if not self.band:
            self.band = _band_from_channel(self.channel)
        if not self.freq_mhz and self.channel:
            self.freq_mhz = _freq_from_channel(self.channel)

    def signal_percent(self) -> int:
        clamped = max(-90, min(-30, self.signal_dbm))
        return int((clamped + 90) / 60 * 100)

    def quality_label(self) -> str:
        if self.signal_dbm >= -50: return "Excellent"
        if self.signal_dbm >= -65: return "Good"
        if self.signal_dbm >= -75: return "Fair"
        return "Poor"

    def signal_color(self) -> str:
        if self.signal_dbm >= -50: return "#00c853"
        if self.signal_dbm >= -65: return "#ffd600"
        if self.signal_dbm >= -75: return "#ff6d00"
        return "#d50000"

    def freq_label(self) -> str:
        return f"{self.freq_mhz} MHz" if self.freq_mhz else (self.band or "-")

    def mode_label(self) -> str:
        return self.mode if self.mode else "-"


# ── Public API ────────────────────────────────────────────────────────────────

def scan(retries: int = 3) -> list[AccessPoint]:
    os_name = platform.system()
    last_err = None
    for attempt in range(retries):
        try:
            if os_name == "Windows": return _scan_windows()
            if os_name == "Linux":   return _scan_linux()
            if os_name == "Darwin":  return _scan_macos()
            raise OSError(f"Unsupported OS: {os_name}")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5)
    raise RuntimeError(f"WiFi scan failed after {retries} attempts: {last_err}")


def scan_averaged(samples: int = 5, delay: float = 0.3) -> list[AccessPoint]:
    all_results: dict[str, list[int]] = {}
    ap_info: dict[str, AccessPoint] = {}
    for i in range(samples):
        try:
            for ap in scan():
                key = ap.bssid.upper()
                if key not in ap_info:
                    all_results[key] = []
                    ap_info[key] = ap
                all_results[key].append(ap.signal_dbm)
        except Exception:
            pass
        if i < samples - 1:
            time.sleep(delay)

    result = []
    for bssid, readings in all_results.items():
        ap = ap_info[bssid]
        avg_dbm = int(sum(readings) / len(readings))
        result.append(AccessPoint(
            ssid=ap.ssid, bssid=ap.bssid, signal_dbm=avg_dbm,
            channel=ap.channel, band=ap.band, freq_mhz=ap.freq_mhz,
            channel_width=ap.channel_width, security=ap.security,
            mode=ap.mode, vendor=ap.vendor,
        ))
    return sorted(result, key=lambda a: a.signal_dbm, reverse=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _band_from_channel(ch: int) -> str:
    if 1 <= ch <= 14:   return "2.4 GHz"
    if 32 <= ch <= 177: return "5 GHz"
    return ""

def _freq_from_channel(ch: int) -> int:
    if 1 <= ch <= 13:   return 2407 + ch * 5
    if ch == 14:        return 2484
    if 32 <= ch <= 177: return 5000 + ch * 5
    return 0

def _mode_from_flags(s: str) -> str:
    u = s.upper()
    if any(x in u for x in ("AX", "HE", "802.11AX", "WIFI 6", "WI-FI 6")):
        return "ax (Wi-Fi 6)"
    if any(x in u for x in ("AC", "VHT", "802.11AC")):
        return "ac (Wi-Fi 5)"
    if any(x in u for x in ("802.11N", " N ", "HT20", "HT40")):
        return "n (Wi-Fi 4)"
    if "802.11G" in u or " G " in u: return "g"
    if "802.11A" in u or " A " in u: return "a"
    if "802.11B" in u or " B " in u: return "b"
    return "-"

def _channel_width_from_str(s: str) -> str:
    s = s.strip()
    for w in ("160", "80+80", "80", "40", "20"):
        if w in s: return f"{w} MHz"
    return s or "-"

def _security_from_flags(auth: str, enc: str = "") -> str:
    a, e = auth.upper(), enc.upper()
    if "WPA3" in a:
        return "WPA2/WPA3-Personal" if "WPA2" in a else "WPA3-Personal"
    if "WPA2" in a or "RSN" in a:
        return "WPA2-Enterprise" if any(x in a for x in ("ENTERPRISE","EAP","802.1X")) else "WPA2-Personal"
    if "WPA" in a: return "WPA-Personal"
    if "WEP" in e or "WEP" in a: return "WEP (insecure)"
    if "OPEN" in a or (not a.strip() and not e.strip()): return "Open"
    return auth.strip() or "-"


# ── Windows ───────────────────────────────────────────────────────────────────

def _scan_windows() -> list[AccessPoint]:
    result = subprocess.run(
        ["netsh", "wlan", "show", "networks", "mode=bssid"],
        capture_output=True, text=True, timeout=15
    )
    aps = []
    for block in re.split(r'\nSSID \d+ :', result.stdout)[1:]:
        ssid_m   = re.search(r'^\s*(.+)',                             block)
        bssid_m  = re.search(r'BSSID \d+\s*:\s*([0-9a-fA-F:]{17})', block)
        signal_m = re.search(r'Signal\s*:\s*(\d+)%',                 block)
        chan_m   = re.search(r'Channel\s*:\s*(\d+)',                  block)
        auth_m   = re.search(r'Authentication\s*:\s*(.+)',            block)
        enc_m    = re.search(r'Encryption\s*:\s*(.+)',                block)
        radio_m  = re.search(r'Radio type\s*:\s*(.+)',                block)
        if not (ssid_m and bssid_m and signal_m):
            continue
        ssid       = ssid_m.group(1).strip()
        bssid      = bssid_m.group(1).upper()
        signal_pct = int(signal_m.group(1))
        signal_dbm = (signal_pct // 2) - 100
        channel    = int(chan_m.group(1)) if chan_m else 0
        auth       = auth_m.group(1).strip() if auth_m else ""
        enc        = enc_m.group(1).strip()  if enc_m  else ""
        radio      = radio_m.group(1).strip() if radio_m else ""
        mode       = _mode_from_flags(radio)
        width      = "80 MHz" if "ac" in mode else ("40 MHz" if channel > 14 else "20 MHz")
        aps.append(AccessPoint(
            ssid=ssid, bssid=bssid, signal_dbm=signal_dbm, channel=channel,
            security=_security_from_flags(auth, enc), mode=mode, channel_width=width,
        ))
    return aps


# ── Linux ─────────────────────────────────────────────────────────────────────

def _scan_linux() -> list[AccessPoint]:
    try:    return _scan_linux_nmcli()
    except: return _scan_linux_iwlist()

def _scan_linux_nmcli() -> list[AccessPoint]:
    fields = "SSID,BSSID,SIGNAL,CHAN,FREQ,SECURITY,WPA-FLAGS,RSN-FLAGS,MODE,CHAN-WIDTH"
    r = subprocess.run(
        ["nmcli", "-t", "-f", fields, "dev", "wifi", "list", "--rescan", "yes"],
        capture_output=True, text=True, timeout=20
    )
    if r.returncode != 0: raise RuntimeError("nmcli failed")
    aps = []
    for line in r.stdout.strip().splitlines():
        parts = [p.replace("\\:", ":") for p in re.split(r'(?<!\\):', line)]
        if len(parts) < 4: continue
        try:
            ssid, bssid = parts[0].strip(), parts[1].upper().strip()
            signal_dbm  = (int(parts[2]) // 2) - 100
            channel     = int(parts[3])
        except (ValueError, IndexError):
            continue
        freq_str  = parts[4].strip() if len(parts) > 4 else ""
        security  = parts[5].strip() if len(parts) > 5 else ""
        wpa_flags = parts[6].strip() if len(parts) > 6 else ""
        rsn_flags = parts[7].strip() if len(parts) > 7 else ""
        mode_raw  = parts[8].strip() if len(parts) > 8 else ""
        width_raw = parts[9].strip() if len(parts) > 9 else ""
        freq_mhz  = 0
        fm = re.search(r'(\d{4,5})', freq_str)
        if fm: freq_mhz = int(fm.group(1))
        combined = (rsn_flags + wpa_flags + security).upper()
        if "WPA3" in combined:
            sec = "WPA2/WPA3-Personal" if "WPA2" in combined else "WPA3-Personal"
        elif "WPA2" in combined or "RSN" in combined:
            sec = "WPA2-Personal"
        elif "WPA" in combined:
            sec = "WPA-Personal"
        elif security.strip() in ("--", ""):
            sec = "Open"
        else:
            sec = security.strip() or "-"
        aps.append(AccessPoint(
            ssid=ssid, bssid=bssid, signal_dbm=signal_dbm, channel=channel,
            freq_mhz=freq_mhz, security=sec,
            mode=_mode_from_flags(mode_raw + " " + freq_str),
            channel_width=_channel_width_from_str(width_raw),
        ))
    return aps

def _scan_linux_iwlist() -> list[AccessPoint]:
    import shutil
    iface = _get_linux_interface()
    cmd = (["sudo", "iwlist", iface, "scan"]
           if shutil.which("sudo") else ["iwlist", iface, "scan"])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    aps = []
    for cell in re.split(r'Cell \d+ -', r.stdout)[1:]:
        ssid_m   = re.search(r'ESSID:"([^"]*)"',               cell)
        bssid_m  = re.search(r'Address:\s*([0-9A-Fa-f:]{17})', cell)
        signal_m = re.search(r'Signal level=(-?\d+)\s*dBm',    cell)
        chan_m   = re.search(r'Channel:(\d+)',                  cell)
        freq_m   = re.search(r'Frequency:(\d+\.\d+)\s*GHz',    cell)
        enc_m    = re.search(r'Encryption key:(on|off)',        cell)
        ies      = " ".join(re.findall(r'IE:\s*(.+)', cell)).upper()
        if not (ssid_m and bssid_m and signal_m): continue
        channel  = int(chan_m.group(1)) if chan_m else 0
        freq_mhz = int(float(freq_m.group(1)) * 1000) if freq_m else 0
        if "WPA2" in ies or "RSN" in ies: sec = "WPA2-Personal"
        elif "WPA" in ies:                sec = "WPA-Personal"
        elif enc_m and enc_m.group(1) == "on": sec = "WEP (insecure)"
        else:                             sec = "Open"
        aps.append(AccessPoint(
            ssid=ssid_m.group(1), bssid=bssid_m.group(1).upper(),
            signal_dbm=int(signal_m.group(1)), channel=channel, freq_mhz=freq_mhz,
            security=sec, mode=_mode_from_flags(ies),
        ))
    return aps

def _get_linux_interface() -> str:
    r = subprocess.run(["iwconfig"], capture_output=True, text=True)
    m = re.search(r'^(\w+)\s+IEEE', r.stdout, re.MULTILINE)
    if m: return m.group(1)
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                m2 = re.match(r'\s*(\w+):', line)
                if m2: return m2.group(1)
    except FileNotFoundError:
        pass
    return "wlan0"


# ── macOS ─────────────────────────────────────────────────────────────────────

def _scan_macos() -> list[AccessPoint]:
    airport = ("/System/Library/PrivateFrameworks/Apple80211.framework"
               "/Versions/Current/Resources/airport")
    r = subprocess.run([airport, "-s"], capture_output=True, text=True, timeout=15)
    aps = []
    for line in r.stdout.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4: continue
        try:
            bssid_idx = next(i for i, p in enumerate(parts)
                             if re.match(r'[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:', p))
        except StopIteration:
            continue
        ssid = " ".join(parts[:bssid_idx])
        bssid = parts[bssid_idx].upper()
        try:
            signal_dbm  = int(parts[bssid_idx + 1])
            channel_str = parts[bssid_idx + 2]
            channel     = int(re.match(r'\d+', channel_str).group())
        except (ValueError, IndexError, AttributeError):
            continue
        remaining    = parts[bssid_idx + 3:]
        security_raw = " ".join(remaining[2:]) if len(remaining) >= 3 else ""
        width = "20 MHz"
        if "160" in channel_str:                   width = "160 MHz"
        elif "80" in channel_str:                  width = "80 MHz"
        elif "+1" in channel_str or "-1" in channel_str: width = "40 MHz"
        aps.append(AccessPoint(
            ssid=ssid, bssid=bssid, signal_dbm=signal_dbm, channel=channel,
            security=_security_from_flags(security_raw),
            mode=_mode_from_flags(security_raw + channel_str),
            channel_width=width,
        ))
    return aps
