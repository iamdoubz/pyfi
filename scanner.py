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

import subprocess
import platform
import re
import time
import statistics
from dataclasses import dataclass
from typing import Optional


# ── OUI vendor table ──────────────────────────────────────────────────────────
_OUI_TABLE: dict[str, str] = {
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
    "000A27": "Apple",    "000A95": "Apple",    "001124": "Apple",
    "001451": "Apple",    "001B63": "Apple",    "001CB3": "Apple",
    "001E52": "Apple",    "001FF3": "Apple",    "0021E9": "Apple",
    "0023DF": "Apple",    "002500": "Apple",    "3C0754": "Apple",
    "70CD60": "Apple",    "A45E60": "Apple",    "F0DBF8": "Apple",
    "001632": "Samsung",  "002339": "Samsung",  "5425EA": "Samsung",
    "8C7712": "Samsung",  "A0821F": "Samsung",
    "001195": "D-Link",   "0015E9": "D-Link",   "001CF0": "D-Link",
    "1C7EE5": "D-Link",   "280DFC": "D-Link",
    "002722": "Ubiquiti", "04186A": "Ubiquiti", "0418D6": "Ubiquiti",
    "24A43C": "Ubiquiti", "44D9E7": "Ubiquiti", "687278": "Ubiquiti",
    "788A20": "Ubiquiti", "802AA8": "Ubiquiti",
    "000B86": "Aruba",    "001A1E": "Aruba",    "20A6CD": "Aruba",
    "6C3B6B": "Aruba",    "94B4CF": "Aruba",
    "F88FCA": "Google",   "1AC487": "Google",   "54607E": "Google",
    "A47733": "Google",   "3C5AB4": "Google",
    "F0272D": "Amazon",   "44650D": "Amazon",   "AC63BE": "Amazon",
    "34D270": "Amazon",
    "001150": "Belkin",   "001CDF": "Belkin",   "0030BD": "Belkin",
    "94103E": "Belkin",   "EC1A59": "Belkin",
    "001E10": "Huawei",   "002568": "Huawei",   "28311C": "Huawei",
    "3440B5": "Huawei",   "4C1FCC": "Huawei",   "6C8D37": "Huawei",
    "788C8A": "Huawei",   "AC853D": "Huawei",
    "0012F0": "Intel",    "001320": "Intel",    "0016EA": "Intel",
    "001E64": "Intel",    "002129": "Intel",
    "000F66": "Qualcomm", "001374": "Qualcomm",
    "000AF7": "Broadcom", "00904C": "Broadcom",
    "000C43": "Ralink",   "00E04C": "Ralink",
    "4C5E0C": "MikroTik", "CC2DE0": "MikroTik", "E48D8C": "MikroTik",
    "B8690E": "MikroTik",
}


def lookup_vendor(bssid: str) -> str:
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
    if any(x in u for x in ("AX", "HE", "802.11AX", "WIFI 6", "WI-FI 6")): return "ax (Wi-Fi 6)"
    if any(x in u for x in ("AC", "VHT", "802.11AC")):                       return "ac (Wi-Fi 5)"
    if any(x in u for x in ("802.11N", " N ", "HT20", "HT40")):              return "n (Wi-Fi 4)"
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
            ("bInRegDomain",        wt.BOOL),
            ("usBeaconPeriod",      wt.USHORT),
            ("ullTimestamp",        ctypes.c_ulonglong),
            ("ullHostTimestamp",    ctypes.c_ulonglong),
            ("usCapabilityInformation", wt.USHORT),
            ("ulChCenterFrequency", wt.ULONG),              # kHz
            ("wlanRateSet",         ctypes.c_ubyte * 16),
            ("ulIeOffset",          wt.ULONG),
            ("ulIeSize",            wt.ULONG),
        ]

    class WLAN_BSS_LIST(ctypes.Structure):
        _fields_ = [("dwTotalSize",      wt.DWORD),
                    ("dwNumberOfItems",  wt.DWORD),
                    ("wlanBssEntries",   WLAN_BSS_ENTRY * 512)]

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

        # Retrieve BSS list
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

            # dot11BssPhyType → mode hint
            # https://docs.microsoft.com/en-us/windows/win32/nativewifi/dot11-phy-type
            phy_type = entry.dot11BssPhyType
            mode = {
                6: "g",              # dot11_phy_type_erp
                7: "a",              # dot11_phy_type_ht (could also be n/5GHz)
                8: "n (Wi-Fi 4)",    # dot11_phy_type_ht
                9: "ac (Wi-Fi 5)",   # dot11_phy_type_vht
               10: "ax (Wi-Fi 6)",   # dot11_phy_type_he
            }.get(phy_type, "-")

            aps.append(AccessPoint(
                ssid=ssid, bssid=bssid, signal_dbm=signal_dbm,
                channel=channel, freq_mhz=freq_mhz,
                mode=mode,
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
        has_he  = bool(re.search(r'HE capabilities', block, re.I))
        has_vht = bool(re.search(r'VHT capabilities', block, re.I))
        has_ht  = bool(re.search(r'HT capabilities',  block, re.I))
        if has_he:      mode = "ax (Wi-Fi 6)"
        elif has_vht:   mode = "ac (Wi-Fi 5)"
        elif has_ht:    mode = "n (Wi-Fi 4)"
        elif freq_mhz and freq_mhz < 3000: mode = "g"
        else:           mode = "a"

        # Channel width
        if re.search(r'channel width: 160', block): width = "160 MHz"
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
