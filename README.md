# G304 Lite

A lightweight, instrument-grade desktop driver for the **Logitech G304 / G305** wireless mouse. It exposes only the four controls that matter, without installing the full Logitech G HUB suite:

- **Battery** — four coarse tiers (Full / Good / Low / Critical)
- **DPI** — four editable presets
- **Power mode** — Performance / Power-saving
- **Report rate** — 1000 / 500 / 250 / 125 Hz

The desktop surface is a self-drawn Tk canvas with antialiased rounded panels and a frosted instrument look. It lives in the system tray and can start with Windows.

> **Platform:** Windows only. Requires a Logitech G304/G305 with its USB receiver. A built-in demo device lets you explore the UI without hardware.

---

## Screenshot

| Main surface | Tray menu |
|---|---|
| ![main UI](assets/screenshot.png) | Right-click the tray icon for DPI / power / report-rate / autostart / exit |

---

## Quick start

### Run the prebuilt binary

1. Download `G304Lite.exe` from the [latest release](https://github.com/BeidouGoan/g304-lite/releases/latest).
2. Double-click it. It starts in **demo mode** so you can try the interface safely.
3. Click **连接真机** (Connect device) to talk to your mouse.

### Run from source

```bash
# Python 3.10+ with tkinter available
python -m venv .venv
.venv\Scripts\activate
pip install hidapi pystray Pillow

python g304lite.py
```

Build the executable:

```bash
pip install pyinstaller
pyinstaller G304Lite.spec --noconfirm
# output: dist/G304Lite.exe
```

---

## Controls

### Main window

| Area | What it does |
|---|---|
| Battery card | Four-tier level from HID++ `0x1000` (coarse, not a precise percentage) |
| DPI card | Four presets; click a tier to write to the sensor. Click **编辑档位** to change values (200–12000) |
| Power mode | Performance = host mode at the chosen rate; Power-saving = fixed 125 Hz |
| Report rate | 1000 / 500 / 250 / 125 Hz; locked to 125 Hz in power-saving mode |
| Autostart | Toggles the `HKCU\...\Run` entry so the app launches at sign-in |
| Event log | Last two status lines |

### Keyboard shortcuts

| Key | Action |
|---|---|
| `1`–`4` | Switch DPI preset |
| `P` | Toggle power mode |
| `R` | Cycle report rate |
| `F5` | Refresh device state |
| `Esc` | Hide to tray |

### Window behaviour

- **—** minimizes to the taskbar.
- **×** hides to the system tray (right-click the tray icon to restore or quit).
- The tray menu exposes: show window, DPI presets, power mode, report rate, autostart, exit.

---

## How it works

The app speaks **HID++ 2.0** directly to the Logitech USB receiver (no vendor SDK, no G HUB):

| Feature | ID | Used for |
|---|---|---|
| Root | `0x0000` | Feature discovery |
| Battery Status | `0x1000` | Coarse battery tier |
| Adjustable DPI | `0x2201` | Read / write sensor DPI |
| Report Rate | `0x8060` | Read / write polling interval (1/2/4/8 ms) |
| Onboard Profiles | `0x8100` | Host / onboard mode switching |

G304/G305 report battery in coarse steps, not precise percentages, so the UI deliberately shows **tier labels** rather than a fake percentage.

The receiver is probed on usage page `0xFF00` / usage `0x0002`. Device probing runs on a background thread with progress and cancellation, so the UI never freezes during a slow or absent device.

### Project layout

| File | Role |
|---|---|
| `g304core.py` | HID++ transport, device discovery, feature reads/writes, demo transport |
| `g304lite.py` | Desktop UI, system tray, autostart, window lifecycle |
| `make_icon.py` | Regenerates the multi-size app/tray icon |
| `make_ui_bg.py` | Regenerates the frosted, rounded background image |
| `G304Lite.spec` | PyInstaller build recipe |
| `assets/` | Icon, tray image, background, screenshot |
| `AGENTS.md` | General working instructions for future contributors and AI agents |

---

## Known limitations

- **Battery is coarse by design.** The mouse reports tiers, not a precise percentage.
- **Write operations are conservative.** Report-rate writes switch from onboard to host mode first, because onboard-profile mode rejects them.
- **No gain/profile macros, RGB, or button remapping.** By design — this is a four-control utility.
- **Windows-only** (`winreg`, `pystray`, HID API).

---

## Privacy

This repository contains no personal data, machine-specific paths, credentials, or captured device identifiers. See `AGENTS.md` for the rules that keep it that way.

---

## License

MIT. See [LICENSE](LICENSE).
