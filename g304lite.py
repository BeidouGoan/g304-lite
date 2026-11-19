"""
G304 Lite - instrument-grade control surface for the Logitech G304.

UI layer: a custom-drawn Tk canvas with layered frosted surfaces, amber signal
colour and tactile states.  An offscreen proxy window owns the real taskbar
button so the drawn surface can stay borderless and rounded.  Device layer
(g304core) and tray/autostart logic are kept intact so visual changes never
compromise the four core controls.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None

try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageTk = None

try:
    from g304core import (
        DeviceNotFound,
        G304Controller,
        HZ_TO_MS,
        MockTransport,
        TIER_NAMES,
        find_controller,
    )
except ImportError:  # pragma: no cover
    from .g304core import (
        DeviceNotFound,
        G304Controller,
        HZ_TO_MS,
        MockTransport,
        TIER_NAMES,
        find_controller,
    )


APP_NAME = "G304 Lite"
W, H = 900, 600

# Instrument palette - tinted neutrals, one signal colour, one status colour.
BG = "#0F1319"          # window surface behind the frosted artwork
KEY = "#000001"         # transparency key: never used by any real UI pixel
HEADER = "#151B23"
PANEL = "#1B222C"
PANEL_ALT = "#222A35"
PRESSED = "#2B3542"
STROKE = "#35404F"
STROKE_SOFT = "#2A333F"
TEXT = "#F3F5F7"
MUTED = "#A8B2C0"
FAINT = "#6E7B8C"
AMBER = "#F2B544"
AMBER_DEEP = "#B67A18"
AMBER_TINT = "#3A2A14"
TEAL = "#4FD1C5"
TEAL_TINT = "#153332"
RED = "#F27777"
DPI_DOTS = [AMBER, "#F6CF7B", "#C99A45", "#6E7787"]
RATE_HZ = [1000, 500, 250, 125]

F_TITLE = ("Bahnschrift SemiBold", 21, "bold")
F_SUB = ("Microsoft YaHei UI", 9)
F_LABEL = ("Microsoft YaHei UI", 10, "bold")
F_BODY = ("Microsoft YaHei UI", 10)
F_SMALL = ("Microsoft YaHei UI", 9)
F_TIER = ("Bahnschrift SemiBold", 31, "bold")
F_NUM = ("Bahnschrift SemiBold", 19, "bold")
F_PILL = ("Microsoft YaHei UI", 9, "bold")
F_LOG = ("Consolas", 9)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "G304Lite"


def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def load_config() -> dict:
    defaults = {"dpi_presets": [400, 800, 2000, 3200], "active_dpi": 2,
                "power_mode": "high", "rate_hz": 1000}
    try:
        with open(config_path(), "r", encoding="utf-8") as handle:
            defaults.update(json.load(handle))
    except Exception:
        pass
    return defaults


def save_config(config: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'


def autostart_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if winreg is None:
        raise RuntimeError("当前系统不支持开机自启")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


class G304LiteApp:
    def __init__(self, surface: tk.Toplevel, proxy: tk.Tk):
        self.root = surface
        self.proxy = proxy
        self.root.title(APP_NAME)
        self.root.configure(bg=KEY)
        self.root.overrideredirect(True)
        self.root.attributes("-transparentcolor", KEY)
        try:
            icon = resource_path("assets/g304-lite.ico")
            self.root.iconbitmap(icon)
            self.proxy.iconbitmap(icon)
        except Exception:
            pass
        self.proxy.title(APP_NAME)
        self.proxy.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        # The offscreen proxy owns the real taskbar button.  Keep the drawn
        # surface in sync when Windows minimises or restores that button.
        self.proxy.bind("<Map>", lambda _e: self.root.after(100, self._sync_surface))
        self.proxy.bind("<Unmap>", lambda _e: self.root.after(100, self._sync_surface))
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.geometry(
            f"{W}x{H}+{(surface.winfo_screenwidth() - W) // 2}+{(surface.winfo_screenheight() - H) // 3}")
        self.root.minsize(W, H)
        self.root.resizable(False, False)

        self.config = load_config()
        self.controller: Optional[G304Controller] = None
        self.device_info = None
        self.link_text = "未连接"
        self.mock = False
        self.state = None
        self.busy = False
        self.busy_kind = ""
        self.cancel_event = threading.Event()
        self.messages: queue.Queue = queue.Queue()
        self.log_lines: list[str] = []
        self.closing = False
        self.tray_icon = None
        self._tray_hint_shown = False
        self._drag_origin = (0, 0)
        self.ui: dict = {}
        self._rect_spec: dict = {}      # canvas item -> (x, y, w, h, radius, fill, outline, width)
        self._round_cache: dict = {}    # (w, h, radius, fill, outline, width) -> PhotoImage

        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.bg_photo = tk.PhotoImage(file=resource_path("assets/ui-bg.png"))
        self.canvas.create_image(0, 0, image=self.bg_photo, anchor="nw")

        self._build_ui()
        self._bind_keys()
        self._build_tray()
        self.root.after(120, self._pump)
        self.root.after(150, self._sync_surface)
        self.root.after(300, lambda: self.connect(mock=True))

    # ---------- canvas primitives ----------
    def _round_photo(self, w, h, radius, fill, outline, width):
        """Return an antialiased rounded-rect image (4x supersampled -> LANCZOS).

        Tk's Canvas polygon has no antialiasing, which makes 6-segment corner
        approximations look jagged.  Rasterising once and caching the PhotoImage
        gives genuinely round, pixel-clean edges for every panel and button.
        """
        key = (w, h, radius, fill, outline, width)
        cached = self._round_cache.get(key)
        if cached is not None:
            return cached
        scale = 4
        img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (0, 0, w * scale - 1, h * scale - 1),
            radius=radius * scale,
            fill=fill,
            outline=outline or fill,
            width=max(1, width * scale),
        )
        img = img.resize((w, h), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        self._round_cache[key] = photo
        return photo

    def _rect(self, x, y, w, h, fill, outline="", radius=12, width=1):
        """Create a rounded surface backed by a cached antialiased image."""
        outline = outline or fill
        item = self.canvas.create_image(
            x, y, image=self._round_photo(w, h, radius, fill, outline, width),
            anchor="nw")
        self._rect_spec[item] = (x, y, w, h, radius, fill, outline, width)
        return item

    def _set_rect(self, item, *, fill=None, outline=None, width=None):
        """Repaint a rounded surface while keeping its cached antialiased edges."""
        spec = self._rect_spec.get(item)
        if spec is None:
            return
        x, y, w, h, radius, old_fill, old_outline, old_width = spec
        fill = old_fill if fill is None else fill
        outline = old_outline if outline is None else outline
        width = old_width if width is None else width
        self._rect_spec[item] = (x, y, w, h, radius, fill, outline, width)
        self.canvas.itemconfigure(
            item, image=self._round_photo(w, h, radius, fill, outline, width))

    def _text(self, x, y, text, font=F_BODY, fill=TEXT, anchor="w", tag=None):
        return self.canvas.create_text(x, y, text=text, font=font, fill=fill,
                                       anchor=anchor, tags=(tag,) if tag else ())

    def _panel(self, x, y, w, h, title, subtitle=""):
        # A single rounded surface is the border.  The old extra highlight line
        # sat on top of the stroke and read as a duplicated top border.
        self._rect(x, y, w, h, PANEL, STROKE, radius=18)
        self._text(x + 20, y + 27, title, F_LABEL, TEXT)
        if subtitle:
            self._text(x + 20, y + 50, subtitle, F_SMALL, MUTED)

    def _button(self, key, x, y, w, h, text, command, *, fill=PANEL_ALT,
                fg=TEXT, outline=STROKE_SOFT, font=F_BODY, radius=10):
        rect = self._rect(x, y, w, h, fill, outline, radius=radius)
        label = self._text(x + w / 2, y + h / 2, text, font, fg, anchor="center")
        btn = {"rect": rect, "label": label, "fill": fill, "fg": fg,
               "outline": outline, "x": x, "y": y, "w": w, "h": h}
        self.ui[key] = btn
        for item in (rect, label):
            self.canvas.tag_bind(item, "<Button-1>", lambda _e, c=command, b=btn: self._press(b, c))
            self.canvas.tag_bind(item, "<Enter>", lambda _e, b=btn: self._hover(b, True))
            self.canvas.tag_bind(item, "<Leave>", lambda _e, b=btn: self._hover(b, False))
        return btn

    def _style_button(self, btn, fill=None, fg=None, outline=None, text=None):
        if fill is not None:
            btn["fill"] = fill
            self._set_rect(btn["rect"], fill=fill)
        if outline is not None:
            btn["outline"] = outline
            self._set_rect(btn["rect"], outline=outline)
        if fg is not None:
            btn["fg"] = fg
            self.canvas.itemconfigure(btn["label"], fill=fg)
        if text is not None:
            self.canvas.itemconfigure(btn["label"], text=text)

    def _hover(self, btn, on: bool):
        base = btn["fill"]
        hover = "#2E3A48" if base in (PANEL_ALT, PRESSED, STROKE_SOFT) else base
        self._set_rect(btn["rect"], fill=hover if on else base)

    def _press(self, btn, command):
        self._set_rect(btn["rect"], fill=PRESSED)
        self.root.after(90, lambda: self._set_rect(btn["rect"], fill=btn["fill"]))
        command()

    # ---------- layout ----------
    def _build_ui(self) -> None:
        c = self.canvas
        header = self._rect(24, 20, W - 48, 66, HEADER, STROKE_SOFT, radius=16)
        # A single header surface keeps one continuous border.  The old second
        # rectangle used HEADER as its own outline, erasing the left half of the
        # stroke.  Dragging is bound to this one surface and gated to the title
        # area so buttons on the right keep their own click behaviour.
        c.tag_bind(header, "<ButtonPress-1>", self._start_move)
        c.tag_bind(header, "<B1-Motion>", self._on_move)
        self.title_items = [
            self._text(46, 45, "G304", F_TITLE, TEXT),
            self._text(152, 48, "LITE", ("Bahnschrift SemiBold", 12, "bold"), AMBER),
            self._text(46, 70, "精密 · 轻量 · 仪器级控制", F_SUB, FAINT),
        ]
        for item in self.title_items:
            c.tag_bind(item, "<ButtonPress-1>", self._start_move)
            c.tag_bind(item, "<B1-Motion>", self._on_move)

        # Right-hand control group: laid out right-to-left so the close button
        # keeps a 16px inset from the header edge (it used to sit 2px away).
        self._button("refresh", 400, 36, 72, 32, "刷新", self.refresh, font=F_SMALL)
        self._button("link", 480, 36, 104, 32, "连接真机", self.toggle_mock, font=F_SMALL)
        self.pill_rect = self._rect(598, 36, 184, 32, AMBER_TINT, AMBER_DEEP, radius=16)
        self.pill_text = self._text(690, 52, "● 未连接", F_PILL, AMBER, anchor="center")
        self._button("minimize", 796, 36, 28, 32, "—", self.minimize_to_taskbar,
                     font=("Segoe UI", 14, "bold"), radius=8)
        self._button("close", 832, 36, 28, 32, "×", self.hide_to_tray,
                     font=("Segoe UI", 14, "bold"), radius=8)

        self._panel(24, 104, 254, 380, "电量", "四档显示 · 原始数据来自 HID++")
        self.battery_tier = self._text(44, 181, "--", F_TIER, TEXT)
        self.battery_bars = [
            self._rect(44 + i * 56, 220, 48, 26, PANEL_ALT, PANEL_ALT, radius=8)
            for i in range(4)
        ]
        self.battery_note = self._text(44, 272, "等待设备响应…", F_SMALL, MUTED)
        c.create_line(44, 314, 258, 314, fill=STROKE)
        self._text(44, 338, "电源模式", F_LABEL, TEXT)
        self._button("power_high", 44, 356, 100, 38, "性能模式",
                     lambda: self.set_power_mode("high"), font=F_SMALL)
        self._button("power_low", 152, 356, 100, 38, "省电模式",
                     lambda: self.set_power_mode("low"), font=F_SMALL)

        self._panel(294, 104, 582, 180, "灵敏度 DPI", "四档预设 · 点击即写入传感器")
        self.dpi_cells = []
        for i in range(4):
            x = 314 + i * 138
            rect = self._rect(x, 166, 124, 84, PANEL_ALT, STROKE_SOFT, radius=12)
            dot = c.create_oval(x + 15, 184, x + 24, 193, fill=DPI_DOTS[i], outline="")
            num = self._text(x + 62, 215, str(self.config["dpi_presets"][i]),
                             F_NUM, TEXT, anchor="center")
            tag = self._text(x + 15, 239, f"档 {i + 1}", F_SMALL, FAINT)
            cell = {"rect": rect, "dot": dot, "num": num, "tag": tag, "index": i}
            self.dpi_cells.append(cell)
            for item in (rect, dot, num, tag):
                c.tag_bind(item, "<Button-1>", lambda _e, idx=i: self.set_dpi(idx))
                c.tag_bind(item, "<Enter>", lambda _e, r=rect: self._set_rect(r, fill=PRESSED))
                c.tag_bind(item, "<Leave>", lambda _e, r=rect: self._set_rect(r, fill=PANEL_ALT))
        edit = self._text(314, 270, "编辑档位  →", F_SMALL, AMBER)
        c.tag_bind(edit, "<Button-1>", lambda _e: self.edit_dpi())

        self._panel(294, 298, 582, 186, "回报率 Hz", "性能模式下可调 · 省电模式固定 125 Hz")
        self.rate_buttons = []
        for i, hz in enumerate(RATE_HZ):
            x = 314 + i * 138
            btn = self._button(f"rate_{hz}", x, 362, 124, 52, str(hz),
                               lambda h=hz: self.set_rate(h), font=F_NUM, radius=12)
            self.rate_buttons.append((btn, hz))
        self.rate_note = self._text(314, 444, "", F_SMALL, FAINT)

        self.check_rect = self._rect(30, 512, 18, 18, PANEL_ALT, STROKE, radius=5)
        self.check_mark = c.create_line(34, 521, 37, 524, 43, 516, fill=AMBER, width=2, state="hidden")
        self.autostart_text = self._text(58, 521, "开机自启", F_BODY, TEXT)
        for item in (self.check_rect, self.check_mark, self.autostart_text):
            c.tag_bind(item, "<Button-1>", lambda _e: self.toggle_autostart())

        # Log panel: 24px baseline rhythm yields ~13px of clear space between
        # 9pt lines, with a matched ~12px bottom inset for even vertical cadence.
        self._rect(294, 494, 582, 92, PANEL, STROKE, radius=14)
        self._text(314, 515, "事件日志", F_LABEL, TEXT)
        self.log_lines_items = [
            self._text(314, 538, "", F_LOG, MUTED, anchor="nw"),
            self._text(314, 562, "", F_LOG, MUTED, anchor="nw"),
        ]
        self._text(30, 548, "快捷键  1–4 DPI  ·  P 电源  ·  R 回报率", F_SMALL, FAINT)
        self._text(30, 564, "F5 刷新  ·  Esc 收起", F_SMALL, FAINT)

        self._draw_battery(0)
        self._paint_selection()
        self._render_autostart()

    def _bind_keys(self) -> None:
        for i in range(4):
            self.root.bind(str(i + 1), lambda _e, idx=i: self.set_dpi(idx))
        self.root.bind("p", lambda _e: self.set_power_mode(
            "low" if self.state and self.state.power_mode == "high" else "high"))
        self.root.bind("r", lambda _e: self._cycle_rate())
        self.root.bind("<F5>", lambda _e: self.refresh())
        self.root.bind("<Escape>", lambda _e: self.hide_to_tray())
        self.root.focus_set()

    def _cycle_rate(self) -> None:
        current = self.state.report_rate_hz if self.state and self.state.report_rate_hz else self.config["rate_hz"]
        idx = RATE_HZ.index(current) if current in RATE_HZ else 0
        self.set_rate(RATE_HZ[(idx + 1) % len(RATE_HZ)])

    def _sync_surface(self) -> None:
        """Mirror the proxy window's taskbar state onto the drawn surface."""
        if self.closing:
            return
        state = self.proxy.state()
        if state in ("iconic", "withdrawn"):
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

    def minimize_to_taskbar(self) -> None:
        self.proxy.iconify()
        self.root.after(80, self._sync_surface)

    def _start_move(self, event) -> None:
        # Only the title region drags the window; controls live to the right.
        if event.x > 400:
            return
        self._drag_origin = (event.x, event.y)
        self._drag_screen = (event.x_root, event.y_root)
        self._drag_window = (self.root.winfo_x(), self.root.winfo_y())

    def _on_move(self, event) -> None:
        if not hasattr(self, "_drag_screen"):
            return
        # Absolute screen-space delta: accumulating winfo_x()+dx every motion
        # event makes the window accelerate away from the cursor.
        dx = event.x_root - self._drag_screen[0]
        dy = event.y_root - self._drag_screen[1]
        self.root.geometry(f"+{self._drag_window[0] + dx}+{self._drag_window[1] + dy}")

    # ---------- canvas state painting ----------
    def _draw_battery(self, bars: int) -> None:
        colour = TEAL if bars >= 3 else AMBER if bars == 2 else RED
        for i, item in enumerate(self.battery_bars):
            self._set_rect(item, fill=colour if i < bars else PANEL_ALT,
                           outline=colour if i < bars else PANEL_ALT)

    def _paint_selection(self) -> None:
        state = self.state
        active_dpi = state.dpi if state and state.dpi else self.config["dpi_presets"][self.config["active_dpi"]]
        for cell in self.dpi_cells:
            i = cell["index"]
            selected = abs(self.config["dpi_presets"][i] - active_dpi) < 50
            self._set_rect(cell["rect"],
                           fill=AMBER_TINT if selected else PANEL_ALT,
                           outline=AMBER if selected else STROKE_SOFT,
                           width=2 if selected else 1)
            self.canvas.itemconfigure(cell["num"], fill=AMBER if selected else TEXT)
        high = state.power_mode == "high" if state else self.config["power_mode"] == "high"
        for value, key in (("high", "power_high"), ("low", "power_low")):
            btn = self.ui[key]
            active = (value == "high") == high
            self._style_button(btn, fill=AMBER if active else PANEL_ALT,
                               fg="#151A20" if active else TEXT,
                               outline=AMBER if active else STROKE_SOFT)
        rate_hz = state.report_rate_hz if state and state.report_rate_hz else self.config["rate_hz"]
        for btn, hz in self.rate_buttons:
            locked = bool(state and state.power_mode == "low") and hz != 125
            selected = hz == rate_hz
            self._style_button(
                btn,
                fill=AMBER_TINT if selected else (PANEL if locked else PANEL_ALT),
                fg=FAINT if locked else (AMBER if selected else TEXT),
                outline=AMBER if selected else STROKE_SOFT,
            )
        self.canvas.itemconfigure(
            self.rate_note,
            text="省电模式已锁定 125 Hz" if not high else "当前性能模式 · 四档可选",
            fill=MUTED if high else AMBER,
        )

    def _render_autostart(self) -> None:
        enabled = autostart_enabled()
        self._set_rect(self.check_rect, fill=AMBER if enabled else PANEL_ALT)
        self.canvas.itemconfigure(self.check_mark, state="normal" if enabled else "hidden")

    def _render_log(self) -> None:
        last = self.log_lines[-2:]
        for index, item in enumerate(self.log_lines_items):
            self.canvas.itemconfigure(item, text=last[index] if index < len(last) else "")

    # ---------- system tray ----------
    def _build_tray(self) -> None:
        if pystray is None or Image is None:
            self._post("error", "缺少托盘依赖，状态栏功能不可用")
            return

        def call(fn: Callable, *args):
            def callback(_icon, _item):
                self.root.after(0, lambda: fn(*args))
            return callback

        def checked_dpi(_item, index: int) -> bool:
            preset = self.config["dpi_presets"][index]
            current = self.state.dpi if self.state and self.state.dpi else self.config["dpi_presets"][self.config["active_dpi"]]
            return abs(preset - current) < 50

        def checked_power(_item, mode: str) -> bool:
            current = self.state.power_mode if self.state else self.config["power_mode"]
            return current == mode

        def checked_rate(_item, hz: int) -> bool:
            current = self.state.report_rate_hz if self.state and self.state.report_rate_hz else self.config["rate_hz"]
            return current == hz

        dpi_menu = pystray.Menu(*[
            pystray.MenuItem(f"{value} DPI · 档 {i + 1}", call(self.set_dpi, i),
                             checked=lambda item, i=i: checked_dpi(item, i), radio=True)
            for i, value in enumerate(self.config["dpi_presets"])
        ])
        power_menu = pystray.Menu(
            pystray.MenuItem("高（性能模式）", call(self.set_power_mode, "high"),
                             checked=lambda item: checked_power(item, "high"), radio=True),
            pystray.MenuItem("低（省电模式）", call(self.set_power_mode, "low"),
                             checked=lambda item: checked_power(item, "low"), radio=True),
        )
        rate_menu = pystray.Menu(*[
            pystray.MenuItem(f"{hz} Hz", call(self.set_rate, hz),
                             checked=lambda item, h=hz: checked_rate(item, h), radio=True)
            for hz in RATE_HZ
        ])
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", call(self.show_window), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("灵敏度 DPI", dpi_menu),
            pystray.MenuItem("电源模式", power_menu),
            pystray.MenuItem("回报率", rate_menu),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", call(self.toggle_autostart),
                             checked=lambda _item: autostart_enabled()),
            pystray.MenuItem("退出", call(self.quit_app)),
        )
        image = Image.open(resource_path("assets/g304-lite.png"))
        self.tray_icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _refresh_tray_menu(self) -> None:
        if self.tray_icon is not None:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def show_window(self) -> None:
        if self.closing:
            return
        self.proxy.deiconify()
        self._sync_surface()

    def hide_to_tray(self) -> None:
        if self.closing:
            return
        self.root.withdraw()
        self.proxy.withdraw()
        if self.tray_icon is not None and not self._tray_hint_shown:
            self._tray_hint_shown = True
            try:
                self.tray_icon.notify("已收进状态栏，右键图标可打开菜单或退出。", APP_NAME)
            except Exception:
                pass
        self._post("info", "窗口已收进状态栏；右键状态栏图标可恢复")

    def quit_app(self) -> None:
        self.closing = True
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.after(0, self.proxy.destroy)

    def toggle_autostart(self) -> None:
        target = not autostart_enabled()
        try:
            set_autostart(target)
            self._post("info", f"开机自启已{'开启' if target else '关闭'}")
        except Exception as exc:
            self._post("error", f"开机自启设置失败：{exc}")
        self._render_autostart()
        self._refresh_tray_menu()

    # ---------- device plumbing ----------
    def _post(self, level: str, text: str) -> None:
        self.messages.put((level, text))

    def _pump(self) -> None:
        try:
            while True:
                _level, text = self.messages.get_nowait()
                self.log_lines.append(text)
                self._render_log()
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _run(self, work: Callable, done: Optional[Callable] = None, kind: str = "操作") -> None:
        if self.busy:
            self._post("info", f"正在{self.busy_kind}，请稍候（探测中可再次点击取消）")
            return
        self.busy = True
        self.busy_kind = kind

        def runner():
            try:
                result = work()
                self.root.after(0, lambda: self._finish(result, done, None))
            except Exception as exc:
                self.root.after(0, lambda: self._finish(None, done, exc))

        threading.Thread(target=runner, daemon=True).start()

    def _finish(self, result, done, error) -> None:
        self.busy = False
        self.busy_kind = ""
        self._style_button(self.ui["link"], text="连接真机" if self.mock else "切回演示")
        if error is not None:
            self._post("error", f"✗ {error}")
            if isinstance(error, DeviceNotFound) and self.root.state() not in ("withdrawn", "iconic"):
                messagebox.showwarning(APP_NAME, str(error))
            return
        if done is not None:
            done(result)

    def connect(self, mock: bool) -> None:
        if self.busy:
            if self.busy_kind == "连接真机":
                self.cancel_event.set()
                self._post("info", "已请求取消连接，正在退出探测…")
            else:
                self._post("info", f"正在{self.busy_kind}，请稍候")
            return
        self.cancel_event = threading.Event()
        kind = "连接演示设备" if mock else "连接真机"
        if not mock:
            self._style_button(self.ui["link"], text="取消连接")
            self._post("info", "开始探测罗技 HID 接口…")
        self._run(lambda: self._open(mock), self._after_connect, kind=kind)

    def _open(self, mock: bool):
        if mock:
            transport = MockTransport()
            controller = G304Controller(transport)
            controller.discover()
            return controller, None, "演示设备（模拟 G304）"
        controller, info, link = find_controller(
            progress=lambda message: self._post("info", message),
            cancel=self.cancel_event,
        )
        return controller, info, f"真实设备 PID {info.pid_text} · {link}"

    def _after_connect(self, result) -> None:
        self.controller, self.device_info, self.link_text = result
        self.mock = isinstance(self.controller.transport, MockTransport)
        label = "演示模式" if self.mock else "已连接 C53F"
        if not self.mock:
            label = "已连接 " + (self.link_text.split("PID ")[-1].split(" ")[0] if "PID " in self.link_text else "设备")
        self._set_rect(self.pill_rect, fill=AMBER_TINT if self.mock else TEAL_TINT,
                       outline=AMBER_DEEP if self.mock else TEAL)
        self.canvas.itemconfigure(self.pill_text, text="● " + label,
                                  fill=AMBER if self.mock else TEAL)
        self._style_button(self.ui["link"], text="连接真机" if self.mock else "切回演示")
        self._post("info", f"连接：{self.link_text}")
        self.refresh()

    def toggle_mock(self) -> None:
        self.connect(mock=not self.mock)

    def refresh(self) -> None:
        if self.controller is None:
            self._post("error", "尚未连接设备")
            return
        self._run(lambda: self.controller.read_state(), self._after_refresh, kind="刷新状态")

    def _after_refresh(self, state) -> None:
        self.state = state
        self.canvas.itemconfigure(self.battery_tier, text=TIER_NAMES[state.battery_tier])
        self._draw_battery(state.battery_bars)
        if state.battery_percent is None:
            note = "设备未上报电量（接收器可能休眠，动一下鼠标再刷新）"
        else:
            kind = "档位标签" if state.battery_coarse else "百分比"
            note = (f"{state.battery_tier}/4 格 · 原始读数 {state.battery_percent}（{kind}）\n"
                    f"轮询率 {state.report_rate_hz or '--'} Hz · 电量非精确测量")
        self.canvas.itemconfigure(self.battery_note, text=note)
        for err in state.errors:
            self._post("error", f"读取失败 {err}")
        self._paint_selection()
        if self.tray_icon is not None:
            try:
                self.tray_icon.title = f"{APP_NAME} · {TIER_NAMES[state.battery_tier]}"
            except Exception:
                pass
        self._refresh_tray_menu()
        self._post("info", f"状态：电量 {TIER_NAMES[state.battery_tier]}（原始 {state.battery_percent}）"
                           f" · DPI {state.dpi} · {state.report_rate_hz} Hz · {state.power_mode}")

    def set_dpi(self, index: int) -> None:
        if self.controller is None:
            self._post("error", "尚未连接设备，无法设置 DPI")
            return
        dpi = self.config["dpi_presets"][index]
        self._run(lambda: self.controller.set_dpi(dpi),
                  lambda _r: (self.config.update(active_dpi=index), save_config(self.config),
                              self._post("info", f"✓ DPI 已设为 {dpi}"), self.refresh()),
                  kind=f"写入 DPI {dpi}")

    def set_rate(self, hz: int) -> None:
        if self.controller is None:
            self._post("error", "尚未连接设备，无法设置回报率")
            return
        if self.state and self.state.power_mode == "low" and hz != 125:
            self._post("error", "省电模式下回报率固定 125 Hz，请先切换到性能模式")
            return
        ms = HZ_TO_MS[hz]
        self._run(lambda: self.controller.set_report_rate_ms(ms),
                  lambda _r: (self.config.update(rate_hz=hz), save_config(self.config),
                              self._post("info", f"✓ 回报率已设为 {hz} Hz"), self.refresh()),
                  kind=f"写入回报率 {hz} Hz")

    def set_power_mode(self, mode: str) -> None:
        if self.controller is None:
            self._post("error", "尚未连接设备，无法切换电源模式")
            return
        high = mode == "high"
        rate = HZ_TO_MS.get(self.config["rate_hz"], 1)
        self._run(lambda: self.controller.set_power_mode(high, rate),
                  lambda _r: (self.config.update(power_mode=mode), save_config(self.config),
                              self._post("info", f"✓ 电源模式 → {'性能' if high else '省电（固定 125Hz）'}"),
                              self.refresh()),
                  kind="切换电源模式")

    def edit_dpi(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("编辑四档 DPI")
        dialog.configure(bg=HEADER)
        dialog.geometry("340x230")
        dialog.resizable(False, False)
        values = []
        for i in range(4):
            tk.Label(dialog, text=f"档位 {i + 1}", bg=HEADER, fg=TEXT, font=F_BODY).grid(
                row=i, column=0, padx=22, pady=9, sticky="w")
            var = tk.StringVar(value=str(self.config["dpi_presets"][i]))
            tk.Entry(dialog, textvariable=var, width=10, bg=PANEL_ALT, fg=TEXT,
                     insertbackground=AMBER, highlightbackground=STROKE).grid(
                row=i, column=1, padx=10, pady=9)
            values.append(var)

        def apply():
            try:
                parsed = [int(v.get()) for v in values]
                if any(not 200 <= v <= 12000 for v in parsed):
                    raise ValueError
            except ValueError:
                messagebox.showerror(APP_NAME, "DPI 必须是 200 - 12000 的整数", parent=dialog)
                return
            self.config["dpi_presets"] = parsed
            save_config(self.config)
            self._paint_selection()
            self._refresh_tray_menu()
            dialog.destroy()
            self._post("info", f"预设已更新：{parsed}")

        tk.Button(dialog, text="保存", command=apply, bg=AMBER, fg="#151A20",
                  activebackground=AMBER_DEEP, bd=0, font=F_LABEL, padx=18, pady=5).grid(
            row=4, column=0, columnspan=2, pady=14)
        dialog.grab_set()


def main() -> int:
    # Windows does not give an override-redirect Tk window a taskbar button.
    # A 1x1 offscreen window owns that button; the drawn surface stays borderless
    # so the rounded, frosted instrument enclosure can reach the screen edges.
    proxy = tk.Tk()
    proxy.geometry("1x1+-30000+-30000")
    proxy.update_idletasks()
    surface = tk.Toplevel(proxy)
    G304LiteApp(surface, proxy)
    proxy.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
