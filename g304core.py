"""
G304 Lite - HID++ 2.0 transport and device controller.

Protocol references:
  - Logitech HID++ 2.0 draft spec (2012)
  - OpenLogi feature pages: 0x2201 / 0x8060 / 0x1004 / 0x1000 / 0x1001 / 0x8100
  - Real-device verification against G903 HERO (same feature IDs) by the open-source community

The controller is deliberately small: it does exactly the four things the
lightweight driver needs - battery, DPI, power mode, polling rate.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

try:
    import hid
except ImportError:  # pragma: no cover - only used when running from source without deps
    hid = None


REPORT_ID_SHORT = 0x10
REPORT_ID_LONG = 0x11
SHORT_LEN = 7
LONG_LEN = 20
VID_LOGITECH = 0x046D

# G304 ships with a LIGHTSPEED / nano receiver; later bundles use other Logitech
# receivers.  All Logitech receiver PIDs known to carry HID++ are listed here.
RECEIVER_PIDS = {
    0xC52B, 0xC52F, 0xC534, 0xC539, 0xC53A, 0xC53F,
    0xC545, 0xC547, 0xC548,
}

FEATURE_ROOT = 0x0000
FEATURE_BATTERY_STATUS = 0x1000
FEATURE_BATTERY_VOLTAGE = 0x1001
FEATURE_UNIFIED_BATTERY = 0x1004
FEATURE_ADJUSTABLE_DPI = 0x2201
FEATURE_REPORT_RATE = 0x8060
FEATURE_ONBOARD_PROFILES = 0x8100

HIDPP20_ERRORS = {
    0: "NoError", 1: "Unknown", 2: "InvalidArgument", 3: "OutOfRange",
    4: "HwError", 5: "LogitechInternal", 6: "InvalidFeatureIndex",
    7: "InvalidFunctionId", 8: "Busy", 9: "Unsupported",
}

MS_TO_HZ = {1: 1000, 2: 500, 4: 250, 8: 125}
HZ_TO_MS = {v: k for k, v in MS_TO_HZ.items()}

# G304's 0x1000 reading is a 4-step coarse ladder, not a precise percentage.
# Values below come from the HID++ 2.0 spec + Solaar BatteryLevelApproximation.
COARSE_TO_TIER = {90: 4, 50: 3, 20: 2, 5: 1}
TIER_NAMES = {0: "未知", 1: "极低", 2: "偏低", 3: "良好", 4: "满电"}


def reading_to_tier(value: Optional[int], coarse: bool = False) -> int:
    if value is None or value <= 0:
        return 0
    if coarse:
        return COARSE_TO_TIER.get(value, 3 if value >= 30 else 2 if value >= 11 else 1)
    return 4 if value >= 81 else 3 if value >= 30 else 2 if value >= 11 else 1

ONBOARD_MODE = 1
HOST_MODE = 2


class HidppError(RuntimeError):
    def __init__(self, code: int, feature_idx: int, function_id: int):
        self.code = code
        self.name = HIDPP20_ERRORS.get(code, f"0x{code:02x}")
        super().__init__(f"HID++ 错误 0x{code:02x} ({self.name}) @ feature {feature_idx} fn {function_id}")


class DeviceNotFound(RuntimeError):
    pass


@dataclass
class HidDeviceInfo:
    path: bytes
    vendor_id: int
    product_id: int
    product: str = ""
    serial: str = ""
    usage_page: int = 0
    usage: int = 0

    @property
    def pid_text(self) -> str:
        return f"{self.product_id:04X}"


class HidppTransport:
    """Long-report HID++ 2.0 transport over hidapi (Windows/Linux/macOS)."""

    def __init__(self, path: bytes, device_index: int = 1, timeout_ms: int = 400):
        if hid is None:
            raise RuntimeError("缺少 hidapi 模块")
        self.path = path
        self.device_index = device_index
        self.timeout_ms = timeout_ms
        self._dev = None
        self._sw_ids = itertools.cycle(range(1, 16))
        self._lock = threading.RLock()

    @property
    def alive(self) -> bool:
        return self._dev is not None

    def open(self) -> None:
        dev = hid.device()
        dev.open_path(self.path)
        try:
            dev.set_nonblocking(False)
        except Exception:
            pass
        self._dev = dev

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    def _drain(self) -> None:
        # hidapi on Windows treats read(timeout_ms=0) as an INFINITE blocking read.
        # Using 0 here hung the whole connection thread when no event was pending.
        for _ in range(32):
            try:
                data = self._dev.read(LONG_LEN, 1)
            except Exception:
                return
            if not data:
                return

    def exchange(self, feature_idx: int, function_id: int, params: Iterable[int] = ()) -> bytes:
        """Send one long FeatureRequest and return the response payload (16 bytes)."""
        params = list(params)
        if len(params) > 16:
            raise ValueError("HID++ 长报文最多 16 个参数字节")
        with self._lock:
            self._drain()
            sw_id = next(self._sw_ids)
            address = ((function_id & 0x0F) << 4) | (sw_id & 0x0F)
            frame = [REPORT_ID_LONG, self.device_index, feature_idx, address]
            frame += params + [0] * (16 - len(params))
            self._dev.write(bytes(frame))

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                resp = self._dev.read(LONG_LEN, self.timeout_ms)
                if not resp or len(resp) < 6:
                    continue
                if resp[1] != self.device_index:
                    continue
                # HID++ 2.0 error: feature 0xFF, byte3 = failed feature, byte5 = code
                if resp[2] == 0xFF:
                    if resp[4] == address:
                        raise HidppError(resp[5], resp[3], function_id)
                    continue
                # HID++ 1.0 error (0x8F) - seldom used on G-series mice
                if resp[2] == 0x8F:
                    continue
                if resp[2] == feature_idx and resp[3] == address:
                    return bytes(resp[4:20])
            raise TimeoutError(f"feature 0x{feature_idx:02x} fn {function_id} 无响应")


class MockTransport:
    """In-memory device that answers the same requests as a real G304.

    It lets the whole request/response pipeline be verified on a PC without a
    receiver plugged in, and is what the exe falls back to for demonstration.
    """

    def __init__(self):
        self.device_index = 1
        self.alive = True
        self.state = {
            "battery": 90,  # G304's 0x1000 full-tier label, not a precise percentage
            "dpi": 2000,
            "rate_ms": 1,
            "onboard_mode": HOST_MODE,
        }
        self.log: list[str] = []
        self.features = {
            # Real G304: 0x1004 / 0x1001 / 0x1F20 all resolve to index 0.
            FEATURE_BATTERY_STATUS: 0x03,
            FEATURE_ADJUSTABLE_DPI: 0x0A,
            FEATURE_REPORT_RATE: 0x0D,
            FEATURE_ONBOARD_PROFILES: 0x10,
        }
        self._lock = threading.RLock()

    def open(self) -> None:
        self.alive = True

    def close(self) -> None:
        self.alive = False

    def exchange(self, feature_idx: int, function_id: int, params: Iterable[int] = ()) -> bytes:
        params = list(params)
        payload = [0] * 16
        with self._lock:
            self.log.append(f"req  feature={feature_idx:02X} fn={function_id} params={[hex(p) for p in params]}")
            if feature_idx == FEATURE_ROOT and function_id == 0:
                code = (params[0] << 8) | params[1]
                payload[0] = self.features.get(code, 0)
            elif feature_idx in (self.features.get(FEATURE_UNIFIED_BATTERY), self.features.get(FEATURE_BATTERY_STATUS)):
                pct = self.state["battery"]
                payload[0] = pct
                payload[1] = 0x08 if pct >= 75 else 0x04 if pct >= 50 else 0x02 if pct >= 25 else 0x01
                payload[2] = 0
                payload[3] = 0
            elif feature_idx == self.features.get(FEATURE_BATTERY_VOLTAGE):
                pct = self.state["battery"]
                payload[0:2] = [(3500 + pct * 5) >> 8 & 0xFF, (3500 + pct * 5) & 0xFF]
                payload[2] = max(1, (pct + 9) // 10)
            elif feature_idx == self.features.get(FEATURE_ADJUSTABLE_DPI):
                if function_id == 1:
                    values = [400, 800, 2000, 3200]
                    for i, v in enumerate(values):
                        payload[1 + i * 2] = v >> 8
                        payload[2 + i * 2] = v & 0xFF
                elif function_id == 2:
                    dpi = self.state["dpi"]
                    payload[0] = 0
                    payload[1], payload[2] = dpi >> 8, dpi & 0xFF
                    payload[3], payload[4] = dpi >> 8, dpi & 0xFF
                elif function_id == 3:
                    dpi = (params[1] << 8) | params[2]
                    if dpi not in (400, 800, 2000, 3200):
                        raise HidppError(3, feature_idx, function_id)
                    self.state["dpi"] = dpi
            elif feature_idx == self.features.get(FEATURE_REPORT_RATE):
                if function_id == 0:
                    payload[0] = 0x8F  # 1/2/4/8 ms all supported
                elif function_id == 1:
                    payload[0] = self.state["rate_ms"]
                elif function_id == 2:
                    if params[0] not in MS_TO_HZ:
                        raise HidppError(3, feature_idx, function_id)
                    self.state["rate_ms"] = params[0]
            elif feature_idx == self.features.get(FEATURE_ONBOARD_PROFILES):
                if function_id == 1:
                    self.state["onboard_mode"] = params[0]
                elif function_id == 2:
                    payload[0] = self.state["onboard_mode"]
            else:
                raise HidppError(6, feature_idx, function_id)
            self.log.append(f"resp  {bytes(payload[:8]).hex(' ')}")
        return bytes(payload)


@dataclass
class DeviceState:
    battery_percent: Optional[int] = None
    battery_tier: int = 0
    battery_coarse: bool = False
    dpi: Optional[int] = None
    report_rate_ms: Optional[int] = None
    power_mode: str = "unknown"
    onboard_mode: Optional[int] = None
    errors: list[str] = field(default_factory=list)

    @property
    def battery_bars(self) -> int:
        return self.battery_tier

    @property
    def report_rate_hz(self) -> Optional[int]:
        return MS_TO_HZ.get(self.report_rate_ms) if self.report_rate_ms else None


class G304Controller:
    """Small controller that maps the four UI features onto HID++ calls."""

    def __init__(self, transport):
        self.transport = transport
        self._features: dict[int, int] = {}
        self._battery_coarse = False

    def discover(self) -> dict[int, int]:
        wanted = [
            FEATURE_UNIFIED_BATTERY, FEATURE_BATTERY_STATUS, FEATURE_BATTERY_VOLTAGE,
            FEATURE_ADJUSTABLE_DPI, FEATURE_REPORT_RATE, FEATURE_ONBOARD_PROFILES,
        ]
        self._features = {}
        for feature_id in wanted:
            try:
                payload = self.transport.exchange(0, 0, [feature_id >> 8, feature_id & 0xFF, 0])
                idx = payload[0]
                if idx:
                    self._features[feature_id] = idx
            except Exception:
                continue
        return self._features

    def _call(self, feature_id: int, fn: int, params: Iterable[int] = ()) -> bytes:
        idx = self._features.get(feature_id)
        if idx is None:
            raise RuntimeError(f"设备不支持 feature 0x{feature_id:04x}")
        return self.transport.exchange(idx, fn, params)

    def get_battery(self) -> Optional[int]:
        # G304 only answers 0x1000; its value is a coarse 4-step ladder.
        # 0x1004/0x1001 are preferred on devices that really expose a percentage.
        for feature_id, fn in (
            (FEATURE_UNIFIED_BATTERY, 1),
            (FEATURE_BATTERY_STATUS, 0),
        ):
            if feature_id not in self._features:
                continue
            try:
                pct = self._call(feature_id, fn)[0]
                self._battery_coarse = feature_id == FEATURE_BATTERY_STATUS
                # 0x1000 values (5/20/50/90) are ladder labels, never multiply them.
                return max(0, min(100, pct))
            except Exception:
                continue
        if FEATURE_BATTERY_VOLTAGE in self._features:
            try:
                payload = self._call(FEATURE_BATTERY_VOLTAGE, 0)
                level = payload[2] if payload[2] else 0
                self._battery_coarse = True
                return max(0, min(100, level * 10))
            except Exception:
                pass
        return None

    def get_dpi(self) -> Optional[int]:
        try:
            payload = self._call(FEATURE_ADJUSTABLE_DPI, 2, [0, 0, 0])
            return (payload[1] << 8) | payload[2]
        except Exception:
            return None

    def set_dpi(self, dpi: int) -> int:
        """Write DPI and read it back. Falls back to the /50 encoding some G-series firmware uses."""
        if not 200 <= dpi <= 12000:
            raise ValueError("DPI 必须在 200 - 12000 之间")
        try:
            self._call(FEATURE_ADJUSTABLE_DPI, 3, [0, dpi >> 8, dpi & 0xFF])
        except HidppError:
            scaled = max(1, round(dpi / 50))
            self._call(FEATURE_ADJUSTABLE_DPI, 3, [0, scaled >> 8, scaled & 0xFF])
        actual = self.get_dpi()
        if actual is not None and abs(actual - dpi) > 50:
            raise RuntimeError(f"DPI 写入未生效：目标 {dpi}，读回 {actual}")
        return actual if actual is not None else dpi

    def get_report_rate_ms(self) -> Optional[int]:
        try:
            ms = self._call(FEATURE_REPORT_RATE, 1, [0, 0, 0])[0]
            return ms if ms in MS_TO_HZ else None
        except Exception:
            return None

    def get_onboard_mode(self) -> Optional[int]:
        try:
            return self._call(FEATURE_ONBOARD_PROFILES, 2, [0, 0, 0])[0]
        except Exception:
            return None

    def _set_report_rate_once(self, ms: int) -> None:
        if ms not in MS_TO_HZ:
            raise ValueError("轮询率只能是 1/2/4/8 ms")
        self._call(FEATURE_REPORT_RATE, 2, [ms, 0, 0])

    def set_report_rate_ms(self, ms: int) -> int:
        """Set polling rate; G304 refuses writes while the onboard profile owns it."""
        try:
            self._set_report_rate_once(ms)
        except HidppError as exc:
            if exc.code != 2 or FEATURE_ONBOARD_PROFILES not in self._features:
                raise
            # Switch ownership from onboard memory to the host, then retry.
            self._call(FEATURE_ONBOARD_PROFILES, 1, [HOST_MODE, 0, 0])
            self._set_report_rate_once(ms)
        actual = self.get_report_rate_ms()
        if actual is not None and actual != ms:
            raise RuntimeError(f"轮询率写入未生效：目标 {ms}ms，读回 {actual}ms")
        return actual if actual is not None else ms

    def set_power_mode(self, high: bool, performance_rate_ms: int = 1) -> str:
        """HI = host-controlled performance mode; LO = fixed 8 ms endurance mode.

        Logitech exposes no public HID++ 'power mode' feature on the G304.  The
        documented behaviour of LO is exactly 'report rate pinned to 8 ms', so
        the switch is implemented as: HI -> host mode + selected rate, LO -> 8 ms.
        """
        self.set_report_rate_ms(8 if not high else performance_rate_ms)
        return "high" if high else "low"

    def read_state(self) -> DeviceState:
        state = DeviceState()
        state.errors = []
        for name, getter in (
            ("电量", self.get_battery),
            ("DPI", self.get_dpi),
            ("轮询率", self.get_report_rate_ms),
            ("板载模式", self.get_onboard_mode),
        ):
            try:
                value = getter()
                setattr(state, {
                    "电量": "battery_percent", "DPI": "dpi",
                    "轮询率": "report_rate_ms", "板载模式": "onboard_mode",
                }[name], value)
            except Exception as exc:
                state.errors.append(f"{name}: {exc}")
        state.battery_tier = reading_to_tier(state.battery_percent, self._battery_coarse)
        state.battery_coarse = self._battery_coarse
        if state.report_rate_ms == 8:
            state.power_mode = "low"
        elif state.report_rate_ms in MS_TO_HZ:
            state.power_mode = "high"
        return state

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass


def enumerate_devices() -> list[HidDeviceInfo]:
    if hid is None:
        return []
    result = []
    for raw in hid.enumerate(VID_LOGITECH, 0):
        info = HidDeviceInfo(
            path=raw["path"],
            vendor_id=raw.get("vendor_id", VID_LOGITECH),
            product_id=raw.get("product_id", 0),
            product=raw.get("product_string", "") or "",
            serial=raw.get("serial_number", "") or "",
            usage_page=raw.get("usage_page", 0),
            usage=raw.get("usage", 0),
        )
        result.append(info)
    return result


def find_controller(
    device_indexes: Iterable[int] = (1, 0xFF),
    progress: Optional[Callable[[str], None]] = None,
    cancel=None,
) -> tuple[Optional[G304Controller], HidDeviceInfo, str]:
    """Probe HID++ interfaces, fast path first, with progress and cancellation.

    The long-report interface (usage page 0xff00 / usage 2) is the only one that
    answers FeatureRequests; probing all eight receiver interfaces with a full
    feature scan made the GUI look frozen for minutes.
    """
    devices = [d for d in enumerate_devices() if d.product_id not in (0xC231, 0xC232)]
    if not devices:
        raise DeviceNotFound("未发现罗技 HID 设备。请插上 G304 接收器；演示模式可无硬件运行。")

    preferred = [d for d in devices if d.usage_page == 0xFF00 and d.usage == 0x0002]
    others = [d for d in devices if d not in preferred]

    last_error = ""
    for info in preferred + others:
        for index in device_indexes:
            if cancel is not None and cancel.is_set():
                raise DeviceNotFound("连接已取消")
            if progress is not None:
                progress(f"探测 PID {info.pid_text} · usage {info.usage_page:04X}/{info.usage:04X} · 索引 0x{index:02X}")
            transport = None
            try:
                transport = HidppTransport(info.path, device_index=index, timeout_ms=300)
                transport.open()
                # One cheap request to decide whether this interface speaks HID++ 2.0.
                if not transport.exchange(0, 0, [0x22, 0x01, 0])[0]:
                    raise RuntimeError("该接口不响应 HID++ 2.0 特性查询")
                controller = G304Controller(transport)
                features = controller.discover()
                if not any(k in features for k in (FEATURE_ADJUSTABLE_DPI, FEATURE_UNIFIED_BATTERY, FEATURE_BATTERY_STATUS)):
                    raise RuntimeError("设备未提供 DPI / 电量特性")
                return controller, info, f"设备索引 0x{index:02X}"
            except Exception as exc:
                last_error = str(exc)
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass
    raise DeviceNotFound(f"找到 {len(devices)} 个罗技 HID 接口，但没有一个响应 HID++ 2.0。{last_error}")
