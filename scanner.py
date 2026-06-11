"""
scanner.py — Cross-platform WiFi access point scanner.

Scanning backends (in priority order):
  Windows : wlanapi via ctypes  → WlanScan() + WlanGetNetworkBssList()
            Falls back to netsh if wlanapi is unavailable.
  Linux   : iw dev <iface> scan → raw nl80211 / cfg80211 data (0.1 dBm res.)
            Falls back to nmcli, then iwlist.
  macOS   : airport CLI         → reasonably raw RSSI values.

Why this matters vs. netsh / nmcli:
  netsh  maps hardware RSSI through a driver quality % (0-100) that clamps
  everything above ~-55 dBm to 100 %, which converts to exactly -50 dBm.
  wlanapi's WlanGetNetworkBssList() returns the raw dot11_RSSI field from the
  BSS entry struct — no clamping, no rounding, genuine hardware resolution.

  nmcli/NetworkManager caches scan results and smooths values through its own
  abstraction. iw talks directly to the kernel nl80211 layer and returns signal
  in mBm (millibelsmilliwatt) at 0.1 dBm precision, e.g. -6500 = -65.0 dBm.

scan_averaged() uses the MEDIAN of samples rather than the mean. The median
is more robust against the burst spikes that WiFi RSSI produces and better
matches what professional tools such as NetSpot report.
"""

import csv
import subprocess
import platform
import re
import threading
import time
import statistics
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── OUI vendor lookup — IEEE database with built-in fallback ─────────────────

_OUI_CACHE = Path.home() / ".pyfi" / "oui.csv"
_OUI_URL   = "https://standards-oui.ieee.org/oui/oui.csv"
_OUI_MAX_AGE_DAYS = 30

# Small built-in table used until the full IEEE DB loads (or if offline forever)
_OUI_BUILTIN: dict[str, str] = {
    "000C29": "Cisco",    "001A2F": "Cisco",    "00E0F7": "Cisco",
    "00173F": "Linksys",  "001346": "Linksys",
    "001B2F": "Netgear",  "00224C": "Netgear",  "20E52A": "Netgear",
    "9C3DCF": "Netgear",  "A040A0": "Netgear",  "C03F0E": "Netgear",
    "B0487A": "TP-Link",  "C46E1F": "TP-Link",  "F4F26D": "TP-Link",
    "50C7BF": "TP-Link",  "8CAAB5": "TP-Link",  "D46E5C": "TP-Link",
    "A860B6": "TP-Link",  "1027F5": "TP-Link",  "30DE4B": "TP-Link",
    "107B44": "ASUS",     "1C872C": "ASUS",     "2C56DC": "ASUS",
    "305A3A": "ASUS",     "382C4A": "ASUS",     "40167E": "ASUS",
    "50465D": "ASUS",     "6045CB": "ASUS",     "AC220B": "ASUS",
    "000A27": "Apple",    "000A95": "Apple",     "001124": "Apple",
    "001451": "Apple",    "001B63": "Apple",     "001CB3": "Apple",
    "001E52": "Apple",    "001FF3": "Apple",     "0021E9": "Apple",
    "0023DF": "Apple",    "002500": "Apple",     "3C0754": "Apple",
    "70CD60": "Apple",    "A45E60": "Apple",     "F0DBF8": "Apple",
    "001632": "Samsung",  "002339": "Samsung",   "5425EA": "Samsung",
    "8C7712": "Samsung",  "A0821F": "Samsung",
    "001195": "D-Link",   "0015E9": "D-Link",    "001CF0": "D-Link",
    "1C7EE5": "D-Link",   "280DFC": "D-Link",
    "002722": "Ubiquiti", "04186A": "Ubiquiti",  "0418D6": "Ubiquiti",
    "24A43C": "Ubiquiti", "44D9E7": "Ubiquiti",  "687278": "Ubiquiti",
    "788A20": "Ubiquiti", "802AA8": "Ubiquiti",
    "000B86": "Aruba",    "001A1E": "Aruba",     "20A6CD": "Aruba",
    "6C3B6B": "Aruba",    "94B4CF": "Aruba",
    "F88FCA": "Google",   "1AC487": "Google",    "54607E": "Google",
    "A47733": "Google",   "3C5AB4": "Google",
    "F0272D": "Amazon",   "44650D": "Amazon",    "AC63BE": "Amazon",
    "34D270": "Amazon",
    "001150": "Belkin",   "001CDF": "Belkin",    "0030BD": "Belkin",
    "94103E": "Belkin",   "EC1A59": "Belkin",
    "001E10": "Huawei",   "002568": "Huawei",    "28311C": "Huawei",
    "3440B5": "Huawei",   "4C1FCC": "Huawei",    "6C8D37": "Huawei",
    "788C8A": "Huawei",   "AC853D": "Huawei",
    "0012F0": "Intel",    "001320": "Intel",     "0016EA": "Intel",
    "001E64": "Intel",    "002129": "Intel",
    "000F66": "Qualcomm", "001374": "Qualcomm",
    "000AF7": "Broadcom", "00904C": "Broadcom",
    "000C43": "Ralink",   "00E04C": "Ralink",
    "4C5E0C": "MikroTik", "CC2DE0": "MikroTik",  "E48D8C": "MikroTik",
    "B8690E": "MikroTik",
}

_oui_db:   dict[str, str] = {}
_oui_lock: threading.Lock = threading.Lock()


def _parse_oui_csv(path: Path) -> dict[str, str]:
    db: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) >= 3 and len(row[1]) == 6:
                db[row[1].upper()] = row[2].strip()
    return db


def _load_oui_db_worker() -> None:
    global _oui_db
    try:
        needs_download = (
            not _OUI_CACHE.exists()
            or (time.time() - _OUI_CACHE.stat().st_mtime) > _OUI_MAX_AGE_DAYS * 86400
        )
        if needs_download:
            _OUI_CACHE.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(_OUI_URL, _OUI_CACHE)
        db = _parse_oui_csv(_OUI_CACHE)
        with _oui_lock:
            _oui_db = db
    except Exception:
        pass


# Start loading in the background immediately so it's ready before the first scan
threading.Thread(target=_load_oui_db_worker, daemon=True).start()


def lookup_vendor(bssid: str) -> str:
    hexstr = bssid.upper().replace(":", "").replace("-", "")
    # Locally-administered MAC (U/L bit, 0x02, set in the first octet): the OUI
    # is randomized — not a real IEEE vendor assignment — so a lookup is
    # meaningless. These are the virtual/guest/mesh BSSIDs a single radio spawns.
    try:
        if int(hexstr[:2], 16) & 0x02:
            return "Local"
    except ValueError:
        pass
    oui = hexstr[:6]
    with _oui_lock:
        db = _oui_db
    return db.get(oui) or _OUI_BUILTIN.get(oui, "-")


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
            # Prefer freq-based detection — channel numbers overlap between bands
            # (e.g. channel 37 exists in both 5 GHz and 6 GHz)
            if self.freq_mhz:
                self.band = _band_from_freq(self.freq_mhz)
            else:
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
    """Single scan pass using the best available backend for this OS."""
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
                time.sleep(0.3)
    raise RuntimeError(f"WiFi scan failed after {retries} attempts: {last_err}")


def scan_averaged(samples: int = 10, delay: float = 0.1) -> list[AccessPoint]:
    """
    Scan `samples` times with `delay` seconds between each pass.
    For each BSSID, report the MEDIAN signal across all samples that saw it.

    Using the median (vs. mean) discards RF burst spikes and better matches
    the instantaneous values that professional tools report.  10 samples at
    100 ms gives ~1.1 s total and catches several beacon intervals.
    """
    all_readings: dict[str, list[int]] = {}   # bssid -> [dbm, ...]
    ap_info:      dict[str, AccessPoint] = {}

    for i in range(samples):
        try:
            for ap in scan():
                key = ap.bssid.upper()
                if key not in ap_info:
                    all_readings[key] = []
                    ap_info[key] = ap
                all_readings[key].append(ap.signal_dbm)
        except Exception:
            pass
        if i < samples - 1:
            time.sleep(delay)

    result = []
    for bssid, readings in all_readings.items():
        ap = ap_info[bssid]
        # Median is more robust than mean for bursty RF measurements
        median_dbm = int(statistics.median(readings))
        result.append(AccessPoint(
            ssid=ap.ssid, bssid=ap.bssid, signal_dbm=median_dbm,
            channel=ap.channel, band=ap.band, freq_mhz=ap.freq_mhz,
            channel_width=ap.channel_width, security=ap.security,
            mode=ap.mode, vendor=ap.vendor,
        ))
    return sorted(result, key=lambda a: a.signal_dbm, reverse=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _band_from_freq(freq_mhz: int) -> str:
    if 2400 <= freq_mhz <= 2500: return "2.4 GHz"
    if 5150 <= freq_mhz <= 5900: return "5 GHz"
    if 5925 <= freq_mhz <= 7125: return "6 GHz"
    return ""

def _band_from_channel(ch: int) -> str:
    if 1 <= ch <= 14:   return "2.4 GHz"
    if 32 <= ch <= 177: return "5 GHz"
    return ""

def _freq_from_channel(ch: int) -> int:
    if 1 <= ch <= 13:   return 2407 + ch * 5
    if ch == 14:        return 2484
    if 32 <= ch <= 177: return 5000 + ch * 5
    return 0

def _security_from_ies(ie_bytes: bytes) -> str:
    """
    Determine security by walking the raw beacon / probe-response IEs.

    This is the only reliable source for HIDDEN networks: their security can't
    be looked up by SSID via WlanGetAvailableNetworkList, but the RSN/WPA IEs
    are present in the beacon regardless of whether the SSID is broadcast.

    Element 48  (RSN)            → WPA2 / WPA3, with AKM suite type telling us
                                   PSK (Personal) vs SAE (WPA3) vs 802.1X (Enterprise).
    Element 221 (Vendor, OUI
      00:50:F2 type 1)          → legacy WPA1.

    Returns "" when no security IE is found, so the caller can fall back to the
    Privacy capability bit to distinguish Open from "secured but unknown".
    """
    has_rsn = has_wpa1 = has_psk = has_sae = is_ent = is_owe = False
    i, n = 0, len(ie_bytes)
    while i + 2 <= n:
        eid = ie_bytes[i]
        ln  = ie_bytes[i + 1]
        if i + 2 + ln > n:
            break
        body = ie_bytes[i + 2:i + 2 + ln]
        if eid == 48 and ln >= 8:                       # RSN IE (WPA2 / WPA3)
            has_rsn = True
            # RSN body: version[2] group-cipher[4] pairwise-count[2] ...
            # so the pairwise-cipher count starts at offset 6, the AKM list
            # follows the pairwise list.
            pairwise_count = int.from_bytes(body[6:8], "little")
            akm_off = 8 + pairwise_count * 4
            if akm_off + 2 <= len(body):
                akm_count = int.from_bytes(body[akm_off:akm_off + 2], "little")
                for k in range(akm_count):
                    s = akm_off + 2 + k * 4
                    if s + 4 <= len(body):
                        t = body[s + 3]                 # AKM suite selector type
                        if t in (1, 3, 5, 11, 12, 13):  is_ent = True   # 802.1X
                        elif t in (8, 9):               has_sae = True  # SAE/WPA3
                        elif t in (2, 4, 6):            has_psk = True  # PSK/WPA2
                        elif t == 18:                   is_owe = True   # OWE
        elif eid == 221 and ln >= 4 and bytes(body[:4]) == b"\x00\x50\xf2\x01":
            has_wpa1 = True                              # WPA1 vendor IE

        i += 2 + ln

    if is_owe:
        return "OWE (Enhanced Open)"
    if is_ent:
        return "WPA3-Enterprise" if has_sae else "WPA2-Enterprise"
    if has_sae and has_psk:
        return "WPA2/WPA3-Personal"
    if has_sae:
        return "WPA3-Personal"
    if has_rsn:
        return "WPA2-Personal"
    if has_wpa1:
        return "WPA-Personal"
    return ""


def _mode_from_ies(ie_bytes: bytes, freq_mhz: int = 0) -> str:
    """
    Determine the Wi-Fi generation by walking the raw 802.11 Information Elements
    from a beacon / probe response.

    This is the authoritative source for Wi-Fi 7 detection on Windows: the scan
    list's dot11BssPhyType field caps at HE (10) and never reports EHT (11), so
    802.11be can only be recognised from the EHT Capabilities element here.

    Relevant elements (all carried inside Element ID 255 = Element ID Extension):
      Ext ID 108 — EHT Capabilities          → Wi-Fi 7  (802.11be)
      Ext ID 35  — HE Capabilities           → Wi-Fi 6  (802.11ax)
      Ext ID 59  — HE 6 GHz Band Capabilities → confirms 6 GHz operation (6E)
    Plus the classic elements:
      Element 191 — VHT Capabilities → Wi-Fi 5 (802.11ac)
      Element 45  — HT Capabilities  → Wi-Fi 4 (802.11n)
    """
    has_eht = has_he = has_he6 = has_vht = has_ht = False
    i, n = 0, len(ie_bytes)
    while i + 2 <= n:
        eid = ie_bytes[i]
        ln  = ie_bytes[i + 1]
        if i + 2 + ln > n:
            break
        body = ie_bytes[i + 2:i + 2 + ln]
        if eid == 45:
            has_ht = True
        elif eid == 191:
            has_vht = True
        elif eid == 255 and ln >= 1:          # Element ID Extension
            ext = body[0]
            if ext == 108:   has_eht = True
            elif ext == 35:  has_he = True
            elif ext == 59:  has_he6 = True
        i += 2 + ln

    if has_eht:
        return "be (Wi-Fi 7)"
    if has_he:
        if has_he6 or 5925 <= freq_mhz <= 7125:
            return "ax (Wi-Fi 6E)"
        return "ax (Wi-Fi 6)"
    if has_vht:
        return "ac (Wi-Fi 5)"
    if has_ht:
        return "n (Wi-Fi 4)"
    return ""


def _vht_op_width(cw: int, seg0: int, seg1: int) -> str:
    """
    Decode a VHT Operation 'Channel Width / CCFS0 / CCFS1' triple into a label.

    cw: 0 = use HT (20/40), 1 = 80 MHz, 2 = 160 (deprecated), 3 = 80+80 (dep).
    Modern APs signal 160/80+80 with cw=1 plus a non-zero CCFS1 segment, so the
    gap between the two centre-frequency segment indices disambiguates them.
    """
    if cw == 2:
        return "160 MHz"
    if cw == 3:
        return "80+80 MHz"
    if cw == 1:
        if seg1:
            d = abs(seg1 - seg0)
            if d == 8:  return "160 MHz"
            if d > 16:  return "80+80 MHz"
        return "80 MHz"
    return ""        # cw == 0 → width is carried by the HT Operation element


def _width_from_ies(ie_bytes: bytes) -> str:
    """
    Determine the operating channel width from the beacon's *Operation* elements.

    The Windows scan list never exposes channel width, so it is parsed here. A
    Wi-Fi 7 AP also carries HE/VHT/HT operation elements for backward compat,
    so the newest-generation element present wins:
      EHT Operation (Ext 106) → 20/40/80/160/320      (320 MHz is EHT-only)
      HE  Operation (Ext 36)  → 6 GHz info: 20/40/80/160   (Wi-Fi 6E)
                              → embedded VHT info           (Wi-Fi 6 on 5 GHz)
      VHT Operation (El 192)  → 80/160/80+80                (Wi-Fi 5)
      HT  Operation (El 61)   → 20/40                       (Wi-Fi 4)

    Field offsets follow the Linux kernel ieee80211.h layouts. All reads are
    length-guarded slices, so a malformed IE yields "" rather than an error.
    """
    eht = he = vht = ht = ""
    i, n = 0, len(ie_bytes)
    while i + 2 <= n:
        eid = ie_bytes[i]
        ln  = ie_bytes[i + 1]
        if i + 2 + ln > n:
            break
        body = ie_bytes[i + 2:i + 2 + ln]

        if eid == 61 and ln >= 2:                    # HT Operation
            sec  = body[1] & 0x03                     # secondary channel offset
            wide = body[1] & 0x04                     # STA channel width = "any"
            ht = "40 MHz" if (wide and sec in (1, 3)) else "20 MHz"

        elif eid == 192 and ln >= 3:                 # VHT Operation
            vht = _vht_op_width(body[0], body[1], body[2])

        elif eid == 255 and ln >= 1:
            ext = body[0]
            if ext == 36 and ln >= 7:                # HE Operation
                # he_oper_params[4] he_mcs_nss[2] then optional fields at off 7
                params = int.from_bytes(body[1:5], "little")
                off = 7
                if params & 0x4000 and off + 3 <= ln:     # VHT Op Info present
                    he = _vht_op_width(body[off], body[off + 1], body[off + 2])
                    off += 3
                if params & 0x8000:                        # Max Co-Hosted BSSID
                    off += 1
                if params & 0x20000 and off + 2 <= ln:     # 6 GHz Op Info present
                    w = body[off + 1] & 0x03               # control byte, width bits
                    he = {0: "20 MHz", 1: "40 MHz",
                          2: "80 MHz", 3: "160 MHz"}.get(w, he)
            elif ext == 106 and ln >= 7:             # EHT Operation
                # params[1] basic_mcs_nss[4] then EHT Op Info (control at body[6])
                if body[1] & 0x01:                    # EHT Operation Info present
                    w = body[6] & 0x07                # 3-bit channel width
                    eht = {0: "20 MHz", 1: "40 MHz", 2: "80 MHz",
                           3: "160 MHz", 4: "320 MHz"}.get(w, "")

        i += 2 + ln

    return eht or he or vht or ht or ""


def _mode_from_flags(s: str) -> str:
    u = s.upper()
    if any(x in u for x in ("EHT", "802.11BE", "WIFI 7", "WI-FI 7")):       return "be (Wi-Fi 7)"
    if any(x in u for x in ("AX", "HE", "802.11AX", "WIFI 6", "WI-FI 6")): return "ax (Wi-Fi 6)"
    if any(x in u for x in ("AC", "VHT", "802.11AC")):                       return "ac (Wi-Fi 5)"
    if any(x in u for x in ("802.11N", " N ", "HT20", "HT40")):              return "n (Wi-Fi 4)"
    if "802.11G" in u or " G " in u: return "g"
    if "802.11A" in u or " A " in u: return "a"
    if "802.11B" in u or " B " in u: return "b"
    return "-"

def _channel_width_from_str(s: str) -> str:
    s = s.strip()
    for w in ("320", "160", "80+80", "80", "40", "20"):
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


# ══════════════════════════════════════════════════════════════════════════════
# Windows — wlanapi via ctypes (primary), netsh (fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _scan_windows() -> list[AccessPoint]:
    """Try wlanapi first for raw RSSI; fall back to netsh."""
    try:
        return _scan_windows_wlanapi()
    except Exception:
        return _scan_windows_netsh()


def _scan_windows_wlanapi() -> list[AccessPoint]:
    """
    Use WlanScan() + WlanGetNetworkBssList() via ctypes.

    WlanGetNetworkBssList() returns WLAN_BSS_ENTRY structs whose lRssi field
    is the raw hardware RSSI in dBm — no driver quality-% conversion, no
    clamping at -50 dBm.  This is what professional tools like NetSpot use.
    """
    import ctypes
    import ctypes.wintypes as wt

    # ── Load wlanapi.dll ──────────────────────────────────────────────────────
    try:
        wlan = ctypes.windll.wlanapi
    except OSError:
        raise RuntimeError("wlanapi.dll not available")

    # ── WlanOpenHandle ────────────────────────────────────────────────────────
    WLAN_CLIENT_VERSION_XP_SP2 = 1
    WLAN_CLIENT_VERSION_VISTA  = 2
    negotiated = wt.DWORD()
    handle     = wt.HANDLE()

    ret = wlan.WlanOpenHandle(WLAN_CLIENT_VERSION_VISTA, None,
                               ctypes.byref(negotiated),
                               ctypes.byref(handle))
    if ret != 0:
        raise RuntimeError(f"WlanOpenHandle failed: {ret}")

    try:
        return _wlanapi_enumerate_and_scan(wlan, handle)
    finally:
        wlan.WlanCloseHandle(handle, None)


def _wlanapi_enumerate_and_scan(wlan, handle) -> list[AccessPoint]:
    import ctypes
    import ctypes.wintypes as wt

    # ── GUID struct ───────────────────────────────────────────────────────────
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wt.ULONG), ("Data2", wt.USHORT),
                    ("Data3", wt.USHORT), ("Data4", ctypes.c_ubyte * 8)]

    # ── WLAN_INTERFACE_INFO_LIST ──────────────────────────────────────────────
    class WLAN_INTERFACE_INFO(ctypes.Structure):
        _fields_ = [("InterfaceGuid",        GUID),
                    ("strInterfaceDescription", ctypes.c_wchar * 256),
                    ("isState",              wt.DWORD)]

    class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
        _fields_ = [("dwNumberOfItems", wt.DWORD),
                    ("dwIndex",          wt.DWORD),
                    ("InterfaceInfo",    WLAN_INTERFACE_INFO * 64)]

    # ── WLAN_BSS_ENTRY ────────────────────────────────────────────────────────
    # DOT11_SSID is { ULONG uSSIDLength; UCHAR ucSSID[32]; } = 36 bytes.
    # It must NOT be packed as c_ubyte*33 — uSSIDLength is a 4-byte ULONG,
    # not a single byte, so the SSID data starts at offset 4, not offset 1.
    class DOT11_SSID(ctypes.Structure):
        _fields_ = [("uSSIDLength", wt.ULONG),
                    ("ucSSID",      ctypes.c_ubyte * 32)]

    class WLAN_BSS_ENTRY(ctypes.Structure):
        _fields_ = [
            ("dot11Ssid",           DOT11_SSID),
            ("uPhyId",              wt.ULONG),
            ("dot11Bssid",          ctypes.c_ubyte * 6),
            ("dot11BssType",        wt.DWORD),
            ("dot11BssPhyType",     wt.DWORD),
            ("lRssi",               ctypes.c_long),         # ← raw hardware dBm
            ("uLinkQuality",        wt.ULONG),
            ("bInRegDomain",        ctypes.c_ubyte),        # BOOLEAN = 1 byte
            ("usBeaconPeriod",      wt.USHORT),
            ("ullTimestamp",        ctypes.c_ulonglong),
            ("ullHostTimestamp",    ctypes.c_ulonglong),
            ("usCapabilityInformation", wt.USHORT),
            ("ulChCenterFrequency", wt.ULONG),              # kHz
            # WLAN_RATE_SET = { ULONG uRateSetLength; USHORT usRateSet[126]; } = 256 B.
            # Was wrongly c_ubyte*16, which shifted ulIeOffset/ulIeSize by 240 bytes
            # and made them read garbage (the cause of the earlier IE-read crash).
            ("wlanRateSet",         ctypes.c_ubyte * 256),
            ("ulIeOffset",          wt.ULONG),
            ("ulIeSize",            wt.ULONG),
        ]

    class WLAN_BSS_LIST(ctypes.Structure):
        _fields_ = [("dwTotalSize",      wt.DWORD),
                    ("dwNumberOfItems",  wt.DWORD),
                    ("wlanBssEntries",   WLAN_BSS_ENTRY * 512)]

    # ── WLAN_AVAILABLE_NETWORK — carries auth/cipher per SSID ────────────────
    # dot11DefaultAuthAlgorithm values (DOT11_AUTH_ALGORITHM):
    #   1=Open  2=WEP  3=WPA-Ent  4=WPA-PSK  5=WPA-None
    #   6=WPA2-Ent(RSNA)  7=WPA2-PSK(RSNA)
    #   8=WPA3-Ent  9=WPA3-SAE(Personal)  10=OWE  11=WPA3-Ent-192
    class WLAN_AVAILABLE_NETWORK(ctypes.Structure):
        _fields_ = [
            ("strProfileName",              ctypes.c_wchar * 256),
            ("dot11Ssid",                   DOT11_SSID),
            ("dot11BssType",                wt.DWORD),
            ("uNumberOfBssids",             wt.ULONG),
            ("bNetworkConnectable",         wt.BOOL),
            ("wlanNotConnectableReason",    wt.DWORD),
            ("uNumberOfPhyTypes",           wt.ULONG),
            ("dot11PhyTypes",               wt.DWORD * 8),
            ("bMorePhyTypes",               wt.BOOL),
            ("wlanSignalQuality",           wt.ULONG),
            ("bSecurityEnabled",            wt.BOOL),
            ("dot11DefaultAuthAlgorithm",   wt.DWORD),
            ("dot11DefaultCipherAlgorithm", wt.DWORD),
            ("dwFlags",                     wt.DWORD),
            ("dwReserved",                  wt.DWORD),
        ]

    class WLAN_AVAILABLE_NETWORK_LIST(ctypes.Structure):
        _fields_ = [("dwNumberOfItems", wt.DWORD),
                    ("dwIndex",         wt.DWORD),
                    ("Network",         WLAN_AVAILABLE_NETWORK * 256)]

    _AUTH_MAP = {
        1: "Open",             2: "WEP (insecure)",
        3: "WPA-Enterprise",   4: "WPA-Personal",    5: "WPA-Personal",
        6: "WPA2-Enterprise",  7: "WPA2-Personal",
        8: "WPA3-Enterprise",  9: "WPA3-Personal",
       10: "OWE (Enhanced Open)", 11: "WPA3-Enterprise",
    }

    # ── Enumerate interfaces ──────────────────────────────────────────────────
    iface_list_ptr = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
    ret = wlan.WlanEnumInterfaces(handle, None, ctypes.byref(iface_list_ptr))
    if ret != 0:
        raise RuntimeError(f"WlanEnumInterfaces failed: {ret}")

    iface_list = iface_list_ptr.contents
    if iface_list.dwNumberOfItems == 0:
        wlan.WlanFreeMemory(iface_list_ptr)
        raise RuntimeError("No WLAN interfaces found")

    aps: list[AccessPoint] = []

    for i in range(iface_list.dwNumberOfItems):
        iface_guid = iface_list.InterfaceInfo[i].InterfaceGuid

        # Request a fresh scan on this interface
        # WlanScan is async; we wait briefly for the OS to complete it
        wlan.WlanScan(handle, ctypes.byref(iface_guid), None, None, None)
        time.sleep(2.0)   # allow the radio time to complete the scan sweep

        # Build SSID → security string from WlanGetAvailableNetworkList.
        # This API returns DOT11_AUTH_ALGORITHM directly — no raw IE parsing needed.
        ssid_security: dict[str, str] = {}
        net_list_ptr = ctypes.POINTER(WLAN_AVAILABLE_NETWORK_LIST)()
        if wlan.WlanGetAvailableNetworkList(
            handle, ctypes.byref(iface_guid), 0, None, ctypes.byref(net_list_ptr)
        ) == 0:
            net_list = net_list_ptr.contents
            for k in range(net_list.dwNumberOfItems):
                net = net_list.Network[k]
                n_len = net.dot11Ssid.uSSIDLength
                try:
                    n_ssid = bytes(net.dot11Ssid.ucSSID[:n_len]).decode(
                        "utf-8", errors="replace").strip("\x00")
                except Exception:
                    n_ssid = ""
                if n_ssid:
                    ssid_security[n_ssid] = _AUTH_MAP.get(
                        net.dot11DefaultAuthAlgorithm, "-")
            wlan.WlanFreeMemory(net_list_ptr)

        # Retrieve per-BSSID list (raw RSSI, phy type, frequency)
        bss_list_ptr = ctypes.POINTER(WLAN_BSS_LIST)()
        ret = wlan.WlanGetNetworkBssList(
            handle, ctypes.byref(iface_guid),
            None,   # all SSIDs
            3,      # dot11_BSS_type_any
            False,  # bSecurityEnabled (don't filter)
            None,
            ctypes.byref(bss_list_ptr)
        )
        if ret != 0:
            continue

        bss_list = bss_list_ptr.contents

        # Snapshot the whole BSS list buffer once, bounded by dwTotalSize.
        # All IE access below slices this bytes object, so a bad offset yields
        # wrong data — never an out-of-bounds read / crash.
        list_base = ctypes.cast(bss_list_ptr, ctypes.c_void_p).value
        try:
            buf = ctypes.string_at(list_base, bss_list.dwTotalSize)
        except Exception:
            buf = b""

        for j in range(bss_list.dwNumberOfItems):
            entry = bss_list.wlanBssEntries[j]

            # Decode SSID using the correct DOT11_SSID field layout:
            # uSSIDLength is the number of valid bytes in ucSSID (max 32).
            ssid_len   = entry.dot11Ssid.uSSIDLength
            ssid_bytes = bytes(entry.dot11Ssid.ucSSID[:ssid_len])
            try:
                ssid = ssid_bytes.decode("utf-8", errors="replace").strip("\x00")
            except Exception:
                ssid = ""
            if not ssid:
                ssid = "<hidden>"

            # Decode BSSID
            bssid = ":".join(f"{b:02X}" for b in entry.dot11Bssid)

            # Raw RSSI in dBm — no clamping, no quality-% conversion
            signal_dbm = int(entry.lRssi)

            # Frequency in MHz (ulChCenterFrequency is in kHz)
            freq_mhz = entry.ulChCenterFrequency // 1000

            # Map frequency to channel
            channel = _channel_from_freq(freq_mhz)

            # dot11BssPhyType → mode hint (fallback only — see IE override below).
            # https://docs.microsoft.com/en-us/windows/win32/nativewifi/dot11-phy-type
            phy_type = entry.dot11BssPhyType
            mode = {
                4: "a",              # dot11_phy_type_ofdm (legacy 802.11a)
                6: "g",              # dot11_phy_type_erp
                7: "n (Wi-Fi 4)",    # dot11_phy_type_ht
                8: "ac (Wi-Fi 5)",   # dot11_phy_type_vht
                9: "ad",             # dot11_phy_type_dmg (802.11ad)
               10: "ax (Wi-Fi 6)",   # dot11_phy_type_he
               11: "be (Wi-Fi 7)",   # dot11_phy_type_eht (802.11be)
            }.get(phy_type, "-")
            # Wi-Fi 6E is 802.11ax (phy_type 10) operating in the 6 GHz band
            if mode == "ax (Wi-Fi 6)" and 5925 <= freq_mhz <= 7125:
                mode = "ax (Wi-Fi 6E)"

            # Slice this BSS entry's beacon IEs from the bounded snapshot.
            # A bad offset yields empty/wrong bytes — never an out-of-bounds read.
            ie_bytes = b""
            if buf:
                ie_off = (ctypes.addressof(entry) - list_base) + entry.ulIeOffset
                ie_end = ie_off + entry.ulIeSize
                if 0 <= ie_off <= ie_end <= len(buf):
                    ie_bytes = buf[ie_off:ie_end]

            # Authoritative mode from the beacon IEs. The scan list's phy type
            # caps at HE (10) even for 802.11be, so EHT (Wi-Fi 7) is ONLY
            # detectable from the EHT Capabilities element here.
            if ie_bytes:
                ie_mode = _mode_from_ies(ie_bytes, freq_mhz)
                if ie_mode:
                    mode = ie_mode

            # Security: prefer the SSID-keyed auth from WlanGetAvailableNetworkList.
            # Hidden networks aren't in that list (no SSID to key on), so fall back
            # to parsing the RSN/WPA IEs directly, then to the Privacy capability
            # bit (0x10) to at least distinguish "Secured" from "Open".
            security = ssid_security.get(ssid, "")
            if not security and ie_bytes:
                security = _security_from_ies(ie_bytes)
            if not security:
                privacy = bool(entry.usCapabilityInformation & 0x10)
                security = "Secured" if privacy else "Open"

            # Channel width — parsed from the HT/VHT/HE/EHT Operation elements,
            # since the Windows scan list does not report it.
            width = _width_from_ies(ie_bytes) if ie_bytes else ""

            aps.append(AccessPoint(
                ssid=ssid, bssid=bssid, signal_dbm=signal_dbm,
                channel=channel, freq_mhz=freq_mhz,
                channel_width=width, mode=mode, security=security,
            ))

        wlan.WlanFreeMemory(bss_list_ptr)

    wlan.WlanFreeMemory(iface_list_ptr)
    return aps


def _channel_from_freq(freq_mhz: int) -> int:
    """Convert centre frequency (MHz) to 802.11 channel number."""
    if 2412 <= freq_mhz <= 2484:
        if freq_mhz == 2484: return 14
        return (freq_mhz - 2407) // 5
    if 5160 <= freq_mhz <= 5885:
        return (freq_mhz - 5000) // 5
    if 5955 <= freq_mhz <= 7115:   # 6 GHz (Wi-Fi 6E)
        return (freq_mhz - 5950) // 5
    return 0


def _scan_windows_netsh() -> list[AccessPoint]:
    """Legacy fallback: netsh wlan show networks."""
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
        if not (ssid_m and bssid_m and signal_m): continue
        signal_pct = int(signal_m.group(1))
        signal_dbm = (signal_pct // 2) - 100
        channel    = int(chan_m.group(1)) if chan_m else 0
        auth       = auth_m.group(1).strip() if auth_m else ""
        enc        = enc_m.group(1).strip()  if enc_m  else ""
        radio      = radio_m.group(1).strip() if radio_m else ""
        mode       = _mode_from_flags(radio)
        width      = "80 MHz" if "ac" in mode else ("40 MHz" if channel > 14 else "20 MHz")
        aps.append(AccessPoint(
            ssid=ssid_m.group(1).strip(), bssid=bssid_m.group(1).upper(),
            signal_dbm=signal_dbm, channel=channel,
            security=_security_from_flags(auth, enc), mode=mode, channel_width=width,
        ))
    return aps


# ══════════════════════════════════════════════════════════════════════════════
# Linux — iw (primary), nmcli (fallback), iwlist (last resort)
# ══════════════════════════════════════════════════════════════════════════════

def _scan_linux() -> list[AccessPoint]:
    try:    return _scan_linux_iw()
    except Exception:
        try:    return _scan_linux_nmcli()
        except: return _scan_linux_iwlist()


def _scan_linux_iw() -> list[AccessPoint]:
    """
    Use `iw dev <iface> scan` to get raw nl80211 data.

    Signal is reported in mBm (100ths of a dBm), e.g. -6500 mBm = -65.0 dBm.
    This bypasses NetworkManager's scan cache and gives genuine per-frame RSSI.
    Requires either root or CAP_NET_RAW on the Python binary.
    """
    import shutil
    iface = _get_linux_interface()

    # Trigger a fresh scan; iw scan blocks until complete
    sudo = ["sudo"] if shutil.which("sudo") else []
    result = subprocess.run(
        sudo + ["iw", "dev", iface, "scan"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0 and "Operation not permitted" in result.stderr:
        raise PermissionError("iw scan requires root or CAP_NET_RAW")
    if result.returncode != 0:
        raise RuntimeError(f"iw scan failed: {result.stderr.strip()}")

    return _parse_iw_output(result.stdout)


def _parse_iw_output(text: str) -> list[AccessPoint]:
    """
    Parse the output of `iw dev <iface> scan`.

    Key fields extracted:
      BSS <bssid>             — start of a new AP block
      SSID: <value>
      signal: <value> dBm     — already in dBm (converted from mBm internally)
      freq: <MHz>
      * primary channel: <n>
      * secondary channel offset: ...  → infer width
      RSN / WPA               — security
      HT capabilities / VHT capabilities / HE capabilities → mode
    """
    aps: list[AccessPoint] = []
    blocks = re.split(r'\nBSS ', text)

    for block in blocks[1:]:
        bssid_m  = re.match(r'([0-9a-fA-F:]{17})', block)
        ssid_m   = re.search(r'SSID: (.*)',                   block)
        signal_m = re.search(r'signal: (-?\d+\.\d+|\d+) dBm', block)
        freq_m   = re.search(r'freq: (\d+)',                   block)
        chan_m   = re.search(r'DS Parameter set: channel (\d+)', block)

        if not (bssid_m and signal_m): continue

        bssid      = bssid_m.group(1).upper()
        ssid       = ssid_m.group(1).strip() if ssid_m else "<hidden>"
        signal_dbm = int(float(signal_m.group(1)))
        freq_mhz   = int(freq_m.group(1)) if freq_m else 0
        channel    = int(chan_m.group(1)) if chan_m else _channel_from_freq(freq_mhz)

        # Security
        has_rsn  = bool(re.search(r'RSN:', block))
        has_wpa  = bool(re.search(r'WPA:', block))
        has_wpa3 = bool(re.search(r'SAE', block))
        if has_wpa3 and has_rsn:    sec = "WPA2/WPA3-Personal"
        elif has_wpa3:              sec = "WPA3-Personal"
        elif has_rsn:               sec = "WPA2-Personal"
        elif has_wpa:               sec = "WPA-Personal"
        else:                       sec = "Open"

        # Mode from capability IEs
        has_eht = bool(re.search(r'EHT capabilities', block, re.I))
        has_he  = bool(re.search(r'HE capabilities',  block, re.I))
        has_vht = bool(re.search(r'VHT capabilities', block, re.I))
        has_ht  = bool(re.search(r'HT capabilities',  block, re.I))
        if has_eht:                                     mode = "be (Wi-Fi 7)"
        elif has_he and freq_mhz and freq_mhz >= 5925: mode = "ax (Wi-Fi 6E)"
        elif has_he:                                    mode = "ax (Wi-Fi 6)"
        elif has_vht:                                   mode = "ac (Wi-Fi 5)"
        elif has_ht:                                    mode = "n (Wi-Fi 4)"
        elif freq_mhz and freq_mhz < 3000:             mode = "g"
        else:                                           mode = "a"

        # Channel width
        if re.search(r'channel width: 320',   block): width = "320 MHz"
        elif re.search(r'channel width: 160', block): width = "160 MHz"
        elif re.search(r'channel width: 80',  block): width = "80 MHz"
        elif re.search(r'channel width: 40',  block): width = "40 MHz"
        else:                                         width = "20 MHz"

        aps.append(AccessPoint(
            ssid=ssid, bssid=bssid, signal_dbm=signal_dbm,
            channel=channel, freq_mhz=freq_mhz,
            security=sec, mode=mode, channel_width=width,
        ))

    return aps


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
        except (ValueError, IndexError): continue
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
        if "WPA3" in combined:        sec = "WPA2/WPA3-Personal" if "WPA2" in combined else "WPA3-Personal"
        elif "WPA2" in combined or "RSN" in combined: sec = "WPA2-Personal"
        elif "WPA" in combined:       sec = "WPA-Personal"
        elif security.strip() in ("--", ""): sec = "Open"
        else:                         sec = security.strip() or "-"
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
    cmd = (["sudo", "iwlist", iface, "scan"] if shutil.which("sudo")
           else ["iwlist", iface, "scan"])
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
        if "WPA2" in ies or "RSN" in ies:         sec = "WPA2-Personal"
        elif "WPA" in ies:                        sec = "WPA-Personal"
        elif enc_m and enc_m.group(1) == "on":    sec = "WEP (insecure)"
        else:                                     sec = "Open"
        aps.append(AccessPoint(
            ssid=ssid_m.group(1), bssid=bssid_m.group(1).upper(),
            signal_dbm=int(signal_m.group(1)), channel=channel, freq_mhz=freq_mhz,
            security=sec, mode=_mode_from_flags(ies),
        ))
    return aps


def _get_linux_interface() -> str:
    # Prefer iw-visible interfaces
    try:
        r = subprocess.run(["iw", "dev"], capture_output=True, text=True)
        m = re.search(r'Interface\s+(\S+)', r.stdout)
        if m: return m.group(1)
    except FileNotFoundError:
        pass
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


# ══════════════════════════════════════════════════════════════════════════════
# macOS — airport CLI (already returns reasonably raw RSSI)
# ══════════════════════════════════════════════════════════════════════════════

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
        if "160" in channel_str:                        width = "160 MHz"
        elif "80" in channel_str:                       width = "80 MHz"
        elif "+1" in channel_str or "-1" in channel_str: width = "40 MHz"
        aps.append(AccessPoint(
            ssid=ssid, bssid=bssid, signal_dbm=signal_dbm, channel=channel,
            security=_security_from_flags(security_raw),
            mode=_mode_from_flags(security_raw + channel_str),
            channel_width=width,
        ))
    return aps
