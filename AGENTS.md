# AGENTS.md — General working instructions

This file is the single source of truth for how to work on this repository. Read it before editing code, running the app, or preparing a release. Keep it tool-agnostic: it applies to humans and AI assistants alike.

## 1. What this project is

A small Windows desktop utility that talks HID++ 2.0 to a Logitech G304/G305 receiver. It exposes four controls (battery tier, DPI, power mode, report rate) with a self-drawn Tkinter UI and a system-tray presence. There is deliberately no vendor SDK dependency.

Design intent: **instrument-grade, precise, restrained**. See `.impeccable.md` for the design context (user, tone, aesthetic direction, principles).

## 2. Non-negotiable rules

### Privacy — never commit these

- Personal names or user handles in docs/comments (use "user" / "G304 user").
- Machine-specific absolute paths (user home directories, workspace paths).
- Runtime config files (`%APPDATA%\G304 Lite\config.json`) or capture scripts that embed real device PIDs/serials.
- Debug screenshots of the desktop, taskbar, tray neighbourhood, or logs that reveal personal applications. Only `assets/screenshot.png` (the clean app window) is allowed in the repo.
- Credentials, tokens, API keys of any kind.

If you need to share a screenshot, crop it to the application window only.

### Functionality over decoration

The four core controls must stay reachable in one step on the main surface. Visual changes must not break: taskbar button, minimize-to-taskbar, close-to-tray, tray context menu, autostart toggle, or keyboard shortcuts. Re-run the interaction checks after any UI edit.

### Device writes are conservative

- Do not "test" write operations against a real mouse without explicit user consent — it changes their hardware config.
- Use the demo transport for UI/regression work.
- Report-rate writes must switch onboard → host mode first (`0x8100`); onboard mode rejects `0x8060` writes.

## 3. Architecture notes (learned the hard way)

### HID transport
- Windows hidapi treats `read(timeout_ms=0)` as an **infinite block**. Always drain with a non-zero timeout (`read(LONG_LEN, 1)`), or the connection thread hangs forever.
- Probe the receiver on usage page `0xFF00`, usage `0x0002`, index `0x01` first (fast path). Skip Logitech virtual PIDs `C231`/`C232`.
- Device probing runs in a worker thread with progress + cancellation; never block the UI thread.
- Battery `0x1000` on G304/G305 is a **coarse tier** (90/50/20/5), not a percentage. Do not render a fake `%`.

### Window / taskbar (Windows 10 LTSC 19044 verified)
- `DwmSetWindowAttribute` cannot force a dark native title bar on this OS/app-theme combination — do not retry it.
- A borderless (`overrideredirect`) Tk window gets **no taskbar button**, and `WS_EX_APPWINDOW` hacks do not fix it.
- Working pattern: an offscreen 1×1 `Tk` "proxy" owns the real taskbar button; a borderless `Toplevel` surface draws the UI. Bind the proxy's `<Map>`/`<Unmap>` to show/hide the surface.
  - custom minimize → `proxy.iconify()`; close → withdraw both; tray restore → `proxy.deiconify()` then sync.
- Transparent key is `#000001`; the background PNG fills its corners with that key. Never reuse that colour for real UI pixels.
- Drag uses **absolute screen coordinates** (`x_root/y_root` delta from press origin). Accumulating `winfo_x()+dx` makes the window accelerate away from the cursor.

### UI rendering
- Rounded surfaces are antialiased images: PIL 4× supersample → LANCZOS → `ImageTk.PhotoImage`, cached by `(w,h,radius,fill,outline,width)`.
- All recoloring goes through `_set_rect()` / `_round_photo()`. Do **not** call `canvas.itemconfigure(item, fill=...)` on a rounded surface — it bypasses the cache and breaks antialiasing.
- A rounded surface carries its own single border. Do not add a separate highlight `create_line` on top of it (that reads as a duplicated border).
- Line rhythm: title→first line ≈18px, log line spacing ≈24px baseline (≈13px visual gap), last line→panel bottom ≈12px.
- Right-hand header controls are laid out right-to-left with a 16px inset from the header edge; keep that inset.

## 4. Common operations

```bash
# Run in demo mode (no hardware needed)
python g304lite.py

# Build the one-file executable
pyinstaller G304Lite.spec --noconfirm
# -> dist/G304Lite.exe

# Regenerate assets after changing their generators
python make_icon.py
python make_ui_bg.py
```

Dependencies: `hidapi`, `pystray`, `Pillow`, `pyinstaller` (build only). Python 3.10+ with tkinter.

## 5. Verification before calling anything done

1. Run `python g304lite.py` and confirm: rounded corners clean, header border continuous on both halves, no duplicated panel borders, spacing even.
2. Exercise: DPI preset click, power-mode switch (report rate locks to 125 Hz in saving mode), window drag, minimize→taskbar restore, close→tray→restore, tray submenus, autostart toggle.
3. When iterating on screenshots, **confirm the window handle changed** after restarting — a stale instance silently invalidates before/after comparisons. Compare images with a pixel-diff, not by eye.
4. Do not claim write operations verified on real hardware unless you actually performed them with consent.

## 6. Release checklist

- [ ] Version tag `vX.Y.Z` chosen; `README.md` and `AGENTS.md` current.
- [ ] `python -m py_compile` on all `.py` files passes.
- [ ] Rebuild `dist/G304Lite.exe` from current source.
- [ ] Attach the exe to the GitHub release; write release notes from the changelog of merged work.
- [ ] Confirm no personal data, paths, or capture screenshots are in the tree.
