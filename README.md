# 📡 WiFi Heatmap Generator

A cross-platform desktop application for collecting and visualizing WiFi signal
strength across a physical space — with or without a floorplan.

---

## Requirements

- Python 3.10+
- tkinter (usually bundled with Python; on Linux: `sudo apt install python3-tk`)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Launch the GUI
python main.py

# Load an existing session on startup
python main.py --session my_office.json
```

---

## Workflow

1. **Launch** the app. The canvas starts in **grid mode** (no floorplan).
2. *(Optional but recommended)* Click **🖼 Load Floorplan** in the sidebar to
   load a PNG/JPEG image of your floor layout. This significantly increases the
   accuracy and usefulness of the heatmap.
3. Click **🔄 Refresh Scan** (right panel) to detect nearby access points.
4. **Select an AP** from the list.
5. Walk to a location in your space and click **▶ Scan & Place**.
   The app scans WiFi (averaged over 5 readings) and waits for you to
   click your current position on the canvas.
6. Repeat step 5 from **at least 6–10 different locations** for a good heatmap.
   Cover corners and edges — they matter most for interpolation quality.
7. Once you have enough points, the heatmap auto-updates after each scan.
8. Switch between APs to compare coverage.
9. **Export PNG** for sharing, or **Save Session** to continue later.

---

## Platform Notes

| OS       | Scan Method                  | Notes                          |
|----------|------------------------------|--------------------------------|
| Windows  | `netsh wlan show networks`   | No admin required              |
| Linux    | `nmcli` → fallback `iwlist`  | May need `sudo` for `iwlist`   |
| macOS    | `airport` CLI tool           | No admin required              |

### Linux Permissions

If scanning fails on Linux, try:
```bash
# Grant your user permission to scan (preferred)
sudo setcap cap_net_raw+eip $(which python3)

# Or run the app with sudo (not recommended long-term)
sudo python main.py
```

---

## Floorplan Tips

- Use a simple overhead floor plan at any scale — even a hand-drawn photo works.
- PNG with transparency supported.
- The image is scaled to fit the canvas automatically.
- **Providing a floorplan increases accuracy** because measurement points map
  to real room features, giving better spatial context for interpolation.

---

## Signal Quality Reference

| dBm range   | Quality   |
|-------------|-----------|
| -30 to -50  | Excellent |
| -50 to -65  | Good      |
| -65 to -75  | Fair      |
| -75 to -90  | Poor      |

---

## Files

| File               | Purpose                                    |
|--------------------|--------------------------------------------|
| `main.py`          | GUI application entry point                |
| `scanner.py`       | Cross-platform WiFi scanning               |
| `interpolator.py`  | Spatial interpolation (RBF / cubic / linear)|
| `renderer.py`      | Matplotlib heatmap rendering               |
| `data.py`          | Session data model and JSON persistence    |
| `requirements.txt` | Python dependencies                        |
