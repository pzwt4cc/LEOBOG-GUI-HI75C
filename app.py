#!/usr/bin/env python3
"""
LEOBOG HI75C RGB Studio v7

Запуск: ./run.sh  (сам поставит venv и зависимости при первом разе)
Автозапуск Hyprland: exec-once = /путь/до/папки/run.sh  в hyprland.conf
Автозапуск других DE: положить run.sh в автозагрузку (Startup Applications)
Роллер/mute не регает: sudo usermod -aG input $USER, затем перелогиниться
Трей не появляется: на Hyprland/Wayland нужен SNI-хост (модуль трея в
    waybar) + пакет libayatana-appindicator3-1 — без этого трей физически
    не может отрисоваться, программа тут ни при чём. Если трей не поднялся,
    кнопка «Свернуть в трей» сама переключается на обычное сворачивание.
"""

import os
import sys
import time
import math
import json
import argparse
import colorsys
import threading
import atexit
import signal
import tempfile
from tkinter import messagebox

import usb.core
import usb.util

try:
    import customtkinter as ctk
except ImportError:
    print("Нужен customtkinter: pip install customtkinter")
    sys.exit(1)

import tkinter as tk

if "PYSTRAY_BACKEND" not in os.environ:
    try:
        import gi
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3  # noqa: F401
        os.environ["PYSTRAY_BACKEND"] = "appindicator"
    except Exception:
        try:
            import gi
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3  # noqa: F401
            os.environ["PYSTRAY_BACKEND"] = "appindicator"
        except Exception:
            pass

import pystray
from PIL import Image, ImageDraw, ImageTk

try:
    import evdev
    from evdev import ecodes
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False

VENDOR_ID       = 0x258a
PRODUCT_ID      = 0x010c
USB_INTERFACE   = 1
PAYLOAD_SIZE    = 520
TOTAL_LED_SLOTS = 170

CMD1        = 0x06
CMD2_CUSTOM = 0x08
HEADER_B6   = 0x7a
HEADER_B7   = 0x01

EFFECTS = {
    "🎨 Сплошной цвет":       {"kind": "solid",      "uses_color": True,  "animated": False, "directional": False, "has_axis": False},
    "🌬️ Дыхание":            {"kind": "breathe",    "uses_color": True,  "animated": True,  "directional": False, "has_axis": False},
    "🌈 Радуга":              {"kind": "rainbow",    "uses_color": False, "animated": True,  "directional": True,  "has_axis": True},
    "🎆 Цветопереход":        {"kind": "colorcycle", "uses_color": False, "animated": True,  "directional": True,  "has_axis": False},
    "🌊 Волна":               {"kind": "wave",       "uses_color": True,  "animated": True,  "directional": True,  "has_axis": True},
    "🐍 Змейка":              {"kind": "snake",      "uses_color": True,  "animated": True,  "directional": True,  "has_axis": True},
    "🐍🐍 Двойная змейка":     {"kind": "dual_snake", "uses_color": True,  "animated": True,  "directional": True,  "has_axis": False},
    "☄️ Комета":              {"kind": "comet",      "uses_color": True,  "animated": True,  "directional": True,  "has_axis": True},
    "💫 Рябь":                {"kind": "ripple",     "uses_color": True,  "animated": True,  "directional": True,  "has_axis": True},
    "✨ Мерцание":            {"kind": "twinkle",    "uses_color": True,  "animated": True,  "directional": False, "has_axis": False},
    "👆 Реакция на нажатие":  {"kind": "reactive",   "uses_color": True,  "animated": True,  "directional": False, "has_axis": False},
}
DEFAULT_EFFECT = "🎨 Сплошной цвет"

# --- Сохранение последнего выбранного режима/цвета между запусками ---
STATE_DIR  = os.path.join(os.path.expanduser("~"), ".config", "leobog-hi75c-studio")
STATE_PATH = os.path.join(STATE_DIR, "state.json")


def _load_saved_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp_path = STATE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp_path, STATE_PATH)
    except Exception as e:
        print(f"[СОСТОЯНИЕ] Не удалось сохранить настройки: {e}")

HEARTBEAT_INTERVAL = 1.0
ANIMATION_FPS       = 24
ANIMATION_INTERVAL  = 1.0 / ANIMATION_FPS
RECONNECT_INTERVAL  = 2.0
WORKER_TICK         = 0.02

BREATH_SPEED      = 1.4
RAINBOW_SPEED     = 0.12
COLORCYCLE_SPEED  = 0.10
WAVE_SPEED        = 0.55
SNAKE_SPEED       = 55.0
SNAKE_LEN         = 18
SNAKE_V_SPEED     = 2.4
SNAKE_V_TRAIL     = 3
COMET_SPEED       = 70.0
COMET_LEN         = 30
COMET_V_SPEED     = 3.0
COMET_V_TRAIL     = 4
RIPPLE_SPEED      = 0.9
RIPPLE_SPREAD     = 3.2
TWINKLE_HZ        = 6.0
REACTIVE_SPEED       = 24.0
REACTIVE_LIFETIME    = 1.1
REACTIVE_RING_W      = 4.0

GRID_ROWS = 6
GRID_COLS = math.ceil(TOTAL_LED_SLOTS / GRID_ROWS)


def _grid_row(i):
    return min(i // GRID_COLS, GRID_ROWS - 1)


ROW_OF = [_grid_row(i) for i in range(TOTAL_LED_SLOTS)]
COL_OF = [i - ROW_OF[i] * GRID_COLS for i in range(TOTAL_LED_SLOTS)]
ROW_INDEX_LIST = [
    [i for i in range(TOTAL_LED_SLOTS) if ROW_OF[i] == r] for r in range(GRID_ROWS)
]

POS_H = [i / TOTAL_LED_SLOTS for i in range(TOTAL_LED_SLOTS)]
POS_V = [ROW_OF[i] / GRID_ROWS for i in range(TOTAL_LED_SLOTS)]

_CENTER_H = (TOTAL_LED_SLOTS - 1) / 2
_SPAN_H = max(1.0, _CENTER_H)
DIST_H = [abs(i - _CENTER_H) / _SPAN_H for i in range(TOTAL_LED_SLOTS)]

_CENTER_V = (GRID_ROWS - 1) / 2
_SPAN_V = max(1.0, _CENTER_V)
DIST_V = [abs(ROW_OF[i] - _CENTER_V) / _SPAN_V for i in range(TOTAL_LED_SLOTS)]


# --- Реальное позиционирование клавиш для эффекта "Реакция на нажатие" ---
# Раньше LED-индекс для кольца брался из хэша кода/символа клавиши. Хэш
# сам по себе не привязан к тому, где клавиша физически находится на
# клавиатуре, а набор кодов, которые реально встречаются при печати,
# довольно узкий — из-за этого хэш почти всегда попадал в одну и ту же
# кучку значений, и кольцо каждый раз "рождалось" из центра или с одного
# края, независимо от того, какая клавиша была нажата на самом деле.
#
# Здесь вместо хэша используется грубая раскладка типичной 75%-клавиатуры:
# 6 физических рядов (столько же, сколько GRID_ROWS) и клавиши слева
# направо в каждом ряду. Кольцо теперь стартует именно там, где клавиша
# физически расположена — слева/справа/сверху/снизу.
KEY_ROWS = [
    ["esc", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12", "delete"],
    ["grave", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "minus", "equal", "backspace"],
    ["tab", "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "bracketleft", "bracketright", "backslash"],
    ["capslock", "a", "s", "d", "f", "g", "h", "j", "k", "l", "semicolon", "apostrophe", "enter"],
    ["shift_l", "z", "x", "c", "v", "b", "n", "m", "comma", "period", "slash", "shift_r", "up"],
    ["ctrl_l", "meta_l", "alt_l", "space", "alt_r", "ctrl_r", "left", "down", "right"],
]

KEY_GRID_POS = {}
for _row_i, _row_keys in enumerate(KEY_ROWS):
    _width = len(_row_keys)
    for _col_i, _key in enumerate(_row_keys):
        KEY_GRID_POS[_key] = (_row_i, _col_i / max(1, _width - 1))


def _logical_key_to_idx(logical):
    """Логическое имя клавиши -> индекс LED-слота в её физическом ряду."""
    pos = KEY_GRID_POS.get(logical)
    if pos is None:
        return None
    row, frac = pos
    row_slots = ROW_INDEX_LIST[row]
    col_i = int(round(frac * (len(row_slots) - 1)))
    return row_slots[col_i]


if EVDEV_AVAILABLE:
    def _build_evdev_key_map():
        m = {}
        for ch in "abcdefghijklmnopqrstuvwxyz":
            code = getattr(ecodes, f"KEY_{ch.upper()}", None)
            if code is not None:
                m[code] = ch
        for d in "0123456789":
            code = getattr(ecodes, f"KEY_{d}", None)
            if code is not None:
                m[code] = d
        for n in range(1, 13):
            code = getattr(ecodes, f"KEY_F{n}", None)
            if code is not None:
                m[code] = f"f{n}"
        extra = {
            "ESC": "esc", "DELETE": "delete", "GRAVE": "grave", "MINUS": "minus",
            "EQUAL": "equal", "BACKSPACE": "backspace", "TAB": "tab",
            "LEFTBRACE": "bracketleft", "RIGHTBRACE": "bracketright", "BACKSLASH": "backslash",
            "CAPSLOCK": "capslock", "SEMICOLON": "semicolon", "APOSTROPHE": "apostrophe",
            "ENTER": "enter", "LEFTSHIFT": "shift_l", "RIGHTSHIFT": "shift_r",
            "COMMA": "comma", "DOT": "period", "SLASH": "slash", "UP": "up",
            "LEFTCTRL": "ctrl_l", "RIGHTCTRL": "ctrl_r", "LEFTALT": "alt_l",
            "RIGHTALT": "alt_r", "LEFTMETA": "meta_l", "RIGHTMETA": "meta_l",
            "SPACE": "space", "LEFT": "left", "DOWN": "down", "RIGHT": "right",
        }
        for name, logical in extra.items():
            code = getattr(ecodes, f"KEY_{name}", None)
            if code is not None:
                m[code] = logical
        return m

    EVDEV_KEY_TO_LOGICAL = _build_evdev_key_map()
else:
    EVDEV_KEY_TO_LOGICAL = {}


def _evdev_code_to_idx(code):
    idx = _logical_key_to_idx(EVDEV_KEY_TO_LOGICAL.get(code))
    if idx is not None:
        return idx
    return int(_hash01(code * 7.919) * TOTAL_LED_SLOTS)  # незнакомая клавиша — старое поведение


_TK_KEYSYM_TO_LOGICAL = {
    "Escape": "esc", "Delete": "delete", "grave": "grave", "minus": "minus",
    "equal": "equal", "BackSpace": "backspace", "Tab": "tab",
    "bracketleft": "bracketleft", "bracketright": "bracketright", "backslash": "backslash",
    "Caps_Lock": "capslock", "semicolon": "semicolon", "apostrophe": "apostrophe",
    "Return": "enter", "KP_Enter": "enter",
    "Shift_L": "shift_l", "Shift_R": "shift_r",
    "comma": "comma", "period": "period", "slash": "slash",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Control_L": "ctrl_l", "Control_R": "ctrl_r",
    "Alt_L": "alt_l", "Alt_R": "alt_r",
    "Super_L": "meta_l", "Super_R": "meta_l",
    "space": "space",
}
for _n in range(1, 13):
    _TK_KEYSYM_TO_LOGICAL[f"F{_n}"] = f"f{_n}"
for _ch in "abcdefghijklmnopqrstuvwxyz":
    _TK_KEYSYM_TO_LOGICAL[_ch] = _ch
    _TK_KEYSYM_TO_LOGICAL[_ch.upper()] = _ch
for _d in "0123456789":
    _TK_KEYSYM_TO_LOGICAL[_d] = _d


def _tk_event_to_idx(event):
    """Индекс LED-слота для события Tkinter <KeyPress>.

    keysym — это уже "переведённый" символ с учётом текущей раскладки
    (для кириллицы это будет что-то вроде 'Cyrillic_a', а не 'a'), так
    что поиск по _TK_KEYSYM_TO_LOGICAL для неё промахивался мимо всех
    английских букв и клавиша попадала в старый случайный хэш-фоллбэк —
    именно поэтому при русской раскладке точка нажатия была "не той".

    На X11/XWayland у Tk-события есть ещё и "сырой" event.keycode — это
    аппаратный скан-код, который почти всегда равен evdev-коду + 8
    (стандартный сдвиг XKB) и НЕ зависит от раскладки. Пробуем его в
    первую очередь — он даёт настоящую физическую позицию клавиши.
    """
    raw_code = getattr(event, "keycode", None)
    if raw_code:
        idx = _logical_key_to_idx(EVDEV_KEY_TO_LOGICAL.get(raw_code - 8))
        if idx is not None:
            return idx
    idx = _logical_key_to_idx(_TK_KEYSYM_TO_LOGICAL.get(event.keysym))
    if idx is not None:
        return idx
    return int(_hash01(hash(event.keysym) * 7.919) * TOTAL_LED_SLOTS)  # неизвестная клавиша — старое поведение


def _scaled(rgb, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def _mix(rgb_a, rgb_b, ratio):
    return tuple(int(a + (b - a) * ratio) for a, b in zip(rgb_a, rgb_b))


def _hash01(n):
    x = math.sin(n * 12.9898) * 43758.5453
    return x - math.floor(x)


def render_frame(effect_name, base_rgb, brightness_pct, t,
                  direction=1, axis="h", speed=1.0, touches=None):
    cfg = EFFECTS[effect_name]
    factor = brightness_pct / 100.0
    kind = cfg["kind"]
    d = 1 if direction >= 0 else -1
    s = max(0.1, speed)
    n = TOTAL_LED_SLOTS
    vertical = axis == "v"

    if kind == "solid":
        return [_scaled(base_rgb, factor)] * n

    if kind == "breathe":
        level = (math.sin(t * BREATH_SPEED * s) + 1) / 2
        return [_scaled(base_rgb, factor * level)] * n

    if kind == "rainbow":
        positions = POS_V if vertical else POS_H
        out = []
        for i in range(n):
            hue = (positions[i] + t * RAINBOW_SPEED * d * s) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, factor)
            out.append((int(r * 255), int(g * 255), int(b * 255)))
        return out

    if kind == "colorcycle":
        hue = (t * COLORCYCLE_SPEED * d * s) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, factor)
        return [(int(r * 255), int(g * 255), int(b * 255))] * n

    if kind == "wave":
        positions = POS_V if vertical else POS_H
        out = []
        for i in range(n):
            phase = positions[i] - t * WAVE_SPEED * d * s
            level = (math.sin(2 * math.pi * phase) + 1) / 2
            out.append(_scaled(base_rgb, factor * level))
        return out

    if kind == "snake":
        out = [(0, 0, 0)] * n
        if vertical:
            head = (t * SNAKE_V_SPEED * d * s) % GRID_ROWS
            for offset in range(SNAKE_V_TRAIL):
                row = int(head - offset * d) % GRID_ROWS
                level = max(0.0, 1.0 - offset / SNAKE_V_TRAIL)
                px = _scaled(base_rgb, factor * level)
                for idx in ROW_INDEX_LIST[row]:
                    out[idx] = px
        else:
            head = (t * SNAKE_SPEED * d * s) % n
            for offset in range(SNAKE_LEN):
                pos = int(head - offset * d) % n
                level = max(0.0, 1.0 - offset / SNAKE_LEN)
                out[pos] = _scaled(base_rgb, factor * level)
        return out

    if kind == "dual_snake":
        out = [(0, 0, 0)] * n
        head_h = (t * SNAKE_SPEED * d * s) % n
        for offset in range(SNAKE_LEN):
            pos = int(head_h - offset * d) % n
            level = max(0.0, 1.0 - offset / SNAKE_LEN)
            candidate = _scaled(base_rgb, factor * level)
            out[pos] = tuple(max(a, b) for a, b in zip(out[pos], candidate))
        head_v = (t * SNAKE_V_SPEED * d * s) % GRID_ROWS
        for offset in range(SNAKE_V_TRAIL):
            row = int(head_v - offset * d) % GRID_ROWS
            level = max(0.0, 1.0 - offset / SNAKE_V_TRAIL)
            candidate = _scaled(base_rgb, factor * level)
            for idx in ROW_INDEX_LIST[row]:
                out[idx] = tuple(max(a, b) for a, b in zip(out[idx], candidate))
        return out

    if kind == "comet":
        out = [(0, 0, 0)] * n
        glow = _mix(base_rgb, (255, 255, 255), 0.55)
        if vertical:
            head = (t * COMET_V_SPEED * d * s) % GRID_ROWS
            for offset in range(COMET_V_TRAIL):
                row = int(head - offset * d) % GRID_ROWS
                level = max(0.0, (1.0 - offset / COMET_V_TRAIL) ** 1.6)
                src = glow if offset == 0 else base_rgb
                px = _scaled(src, factor * level)
                for idx in ROW_INDEX_LIST[row]:
                    out[idx] = px
        else:
            head = (t * COMET_SPEED * d * s) % n
            for offset in range(COMET_LEN):
                pos = int(head - offset * d) % n
                level = max(0.0, (1.0 - offset / COMET_LEN) ** 1.6)
                src = glow if offset == 0 else base_rgb
                out[pos] = _scaled(src, factor * level)
        return out

    if kind == "ripple":
        dist_list = DIST_V if vertical else DIST_H
        out = []
        for i in range(n):
            dist = dist_list[i]
            phase = dist * RIPPLE_SPREAD - t * RIPPLE_SPEED * d * s
            level = (math.sin(2 * math.pi * phase) + 1) / 2
            level *= max(0.0, 1.0 - dist * 0.25)
            out.append(_scaled(base_rgb, factor * level))
        return out

    if kind == "twinkle":
        bucket = int(t * TWINKLE_HZ * s)
        out = []
        for i in range(n):
            h = _hash01(i * 97.13 + bucket * 131.7)
            level = h ** 3
            out.append(_scaled(base_rgb, factor * level))
        return out

    if kind == "reactive":
        # Все 170 LED разбиты на 6 физических рядов последовательными
        # блоками по ~29 штук (0-28, 29-57, 58-86...). Буквенные ряды
        # оказываются во второй половине этого общего диапазона — вправо
        # там почти некуда расширяться (упирается в конец массива), и
        # видно было только движение влево. Цифровой ряд ближе к началу
        # диапазона, поэтому там расширение в обе стороны было заметно
        # и выглядело правильно. Чтобы не зависеть от того, где чей ряд
        # оказался в общем массиве, круг теперь считается ВНУТРИ СВОЕГО
        # РЯДА (по колонке нажатой клавиши), а не по всей ленте сразу —
        # так волна всегда симметрично расходится от точки нажатия.
        out = [(0, 0, 0)] * n
        for origin, t_press in (touches or ()):
            age = t - t_press
            if age < 0 or age > REACTIVE_LIFETIME:
                continue
            radius = age * REACTIVE_SPEED * s
            fade = max(0.0, 1.0 - age / REACTIVE_LIFETIME)
            origin_row = ROW_OF[origin]
            origin_col = COL_OF[origin]
            for i in ROW_INDEX_LIST[origin_row]:
                dist = abs(COL_OF[i] - origin_col)
                delta = abs(dist - radius)
                if delta < REACTIVE_RING_W:
                    level = (1.0 - delta / REACTIVE_RING_W) * fade
                    candidate = _scaled(base_rgb, factor * level)
                    out[i] = tuple(max(a, b) for a, b in zip(out[i], candidate))
        return out

    return [(0, 0, 0)] * n


class ReactiveListener(threading.Thread):
    def __init__(self, controller):
        super().__init__(daemon=True, name="leobog-reactive")
        self.controller = controller
        self._stop_evt = threading.Event()
        self._devices = []

    def _find_devices(self):
        if not EVDEV_AVAILABLE:
            print("[РЕАКЦИЯ] Модуль evdev не установлен (pip install evdev).")
            return []
        exact = []
        keyboard_like = []
        no_permission = []
        all_seen = []
        try:
            paths = evdev.list_devices()
            if not paths:
                print("[РЕАКЦИЯ] /dev/input пуст или недоступен для чтения.")
            for path in paths:
                try:
                    dev = evdev.InputDevice(path)
                except PermissionError:
                    no_permission.append(path)
                    continue
                except Exception:
                    continue
                info = dev.info
                all_seen.append((path, dev.name, info.vendor, info.product))
                if info.vendor == VENDOR_ID and info.product == PRODUCT_ID:
                    exact.append(dev)
                    continue
                try:
                    key_caps = dev.capabilities().get(ecodes.EV_KEY, [])
                except Exception:
                    key_caps = []
                # "клавиатуроподобное" устройство: умеет буквы и пробел —
                # для многих клавиатур ввод регистрируется НЕ под тем же
                # VID/PID, что и кастомный RGB-интерфейс, поэтому точное
                # совпадение часто не находится вовсе.
                if ecodes.KEY_A in key_caps and ecodes.KEY_SPACE in key_caps:
                    keyboard_like.append(dev)
                else:
                    try:
                        dev.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[РЕАКЦИЯ] Не удалось перечислить /dev/input: {e}")

        if exact:
            for d in keyboard_like:
                try:
                    d.close()
                except Exception:
                    pass
            print(f"[РЕАКЦИЯ] Найдено устройство по VID/PID: {[d.path for d in exact]}")
            return exact

        if keyboard_like:
            print(
                "[РЕАКЦИЯ] Точного совпадения VID/PID не найдено, но обнаружены "
                f"клавиатуроподобные устройства — слушаю их: {[d.path for d in keyboard_like]}"
            )
            return keyboard_like

        if no_permission:
            print(
                f"[РЕАКЦИЯ] Нет доступа к {len(no_permission)} устройств(у) в "
                "/dev/input (Permission denied) — среди них мог быть нужный. "
                "Исправление: sudo usermod -aG input $USER, затем перелогиниться "
                "(или перезагрузиться), после чего снова выбрать этот режим."
            )
        print("[РЕАКЦИЯ] Ни одной клавиатуры для чтения не найдено. Видимые устройства:")
        for path, name, vid, pid in all_seen:
            print(f"    {path}: {name!r} VID={vid:#06x} PID={pid:#06x}")
        return []

    def run(self):
        self._devices = self._find_devices()
        if not self._devices:
            print(
                "[РЕАКЦИЯ] Реальные нажатия недоступны (нет evdev, устройство не "
                "найдено, или нет прав на /dev/input). Подсказка: "
                "pip install evdev, sudo usermod -aG input $USER, затем "
                "перелогиниться. Пока имитирую случайные касания."
            )
            self._fallback_loop()
            return
        threads = []
        for dev in self._devices:
            th = threading.Thread(target=self._read_device, args=(dev,), daemon=True)
            th.start()
            threads.append(th)
        while not self._stop_evt.is_set():
            time.sleep(0.2)

    def _read_device(self, device):
        print(f"[РЕАКЦИЯ] Слушаю {device.path} ({device.name})")
        try:
            for event in device.read_loop():
                if self._stop_evt.is_set():
                    break
                if event.type == ecodes.EV_KEY and event.value in (1, 2):
                    self.controller.add_touch(_evdev_code_to_idx(event.code))
                elif event.type == ecodes.EV_REL and event.value != 0:
                    idx = int(_hash01((event.code + 500) * 5.317 + int(time.time() * 5)) * TOTAL_LED_SLOTS)
                    self.controller.add_touch(idx)
        except PermissionError:
            if not self._stop_evt.is_set():
                print(
                    f"[РЕАКЦИЯ] Нет прав на чтение {device.path} (Permission denied). "
                    "sudo usermod -aG input $USER, затем перелогиниться."
                )
        except Exception as e:
            if not self._stop_evt.is_set():
                print(f"[РЕАКЦИЯ] Поток {device.path} прерван: {e}")

    def _fallback_loop(self):
        while not self._stop_evt.is_set():
            idx = int(time.time() * 977) % TOTAL_LED_SLOTS
            self.controller.add_touch(idx)
            time.sleep(1.4)

    def stop(self):
        self._stop_evt.set()
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass


class LeobogController:
    def __init__(self, on_status_change=None):
        self.dev = None
        self.current_rgb = (255, 0, 128)
        self.current_effect = DEFAULT_EFFECT
        self.brightness = 100
        self.direction = 1
        self.axis = "h"
        self.speed = 1.0
        self.touches = []
        self._reactive_listener = None
        self._save_timer = None

        self.running = True
        self._lock = threading.RLock()
        self.on_status_change = on_status_change
        self._last_reported_state = None
        self._start_time = time.monotonic()

        self._load_state()

        atexit.register(self.shutdown)

        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="leobog-worker"
        )
        self._worker.start()

        # Если в прошлый раз был выбран режим "Реакция на нажатие" —
        # слушатель нужно поднять сразу, иначе после автозапуска эффект
        # выглядит выбранным, но не реагирует, пока его не переключить руками.
        if EFFECTS[self.current_effect]["kind"] == "reactive":
            self._start_reactive_listener()

    def _load_state(self):
        saved = _load_saved_state()
        if not saved:
            return
        effect = saved.get("effect")
        if effect in EFFECTS:
            self.current_effect = effect
        rgb = saved.get("rgb")
        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            try:
                self.current_rgb = tuple(max(0, min(255, int(c))) for c in rgb)
            except (TypeError, ValueError):
                pass
        brightness = saved.get("brightness")
        if isinstance(brightness, (int, float)):
            self.brightness = max(0, min(100, int(brightness)))
        direction = saved.get("direction")
        if direction in (1, -1):
            self.direction = direction
        axis = saved.get("axis")
        if axis in ("h", "v"):
            self.axis = axis
        speed = saved.get("speed")
        if isinstance(speed, (int, float)) and speed > 0:
            self.speed = float(speed)

    def _persist_state(self):
        # Слайдеры (яркость/скорость) шлют команду на каждое движение —
        # без задержки это писало бы файл на диск десятки раз в секунду.
        # Поэтому запись откладывается и схлопывается в одну на "успокоение".
        with self._lock:
            state = {
                "effect": self.current_effect,
                "rgb": list(self.current_rgb),
                "brightness": self.brightness,
                "direction": self.direction,
                "axis": self.axis,
                "speed": self.speed,
            }
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.4, _save_state, args=(state,))
        self._save_timer.daemon = True
        self._save_timer.start()

    def _flush_state(self):
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        with self._lock:
            state = {
                "effect": self.current_effect,
                "rgb": list(self.current_rgb),
                "brightness": self.brightness,
                "direction": self.direction,
                "axis": self.axis,
                "speed": self.speed,
            }
        _save_state(state)

    def _try_connect(self):
        try:
            device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            if device is None:
                return None
            try:
                if device.is_kernel_driver_active(USB_INTERFACE):
                    device.detach_kernel_driver(USB_INTERFACE)
            except (usb.core.USBError, NotImplementedError):
                pass
            try:
                device.set_configuration()
            except usb.core.USBError:
                pass
            usb.util.claim_interface(device, USB_INTERFACE)
            return device
        except Exception as e:
            print(f"[ERROR] Подключение не удалось: {e}")
            return None

    def _build_payload(self, t):
        payload = bytearray(PAYLOAD_SIZE)
        payload[0] = CMD1
        payload[1] = CMD2_CUSTOM
        payload[6] = HEADER_B6
        payload[7] = HEADER_B7

        colors = render_frame(
            self.current_effect, self.current_rgb, self.brightness, t,
            direction=self.direction, axis=self.axis, speed=self.speed,
            touches=list(self.touches),
        )
        for slot, (r, g, b) in enumerate(colors):
            idx = 8 + slot * 3
            if idx + 2 >= PAYLOAD_SIZE:
                break
            payload[idx]     = r
            payload[idx + 1] = g
            payload[idx + 2] = b
        return payload

    def _send_locked(self, payload):
        assert len(payload) == PAYLOAD_SIZE, \
            f"[BUG] payload {len(payload)} != {PAYLOAD_SIZE}, отправка отменена"
        if self.dev is None:
            return False
        try:
            self.dev.ctrl_transfer(0x21, 0x09, 0x0306, 0x0001, payload)
            return True
        except usb.core.USBError as e:
            errno = getattr(e, 'errno', None)
            if errno == -7 or errno is None:
                return False
            print(f"[ERROR] Устройство отвалилось (errno={errno}): {e}")
            self._release_locked()
            return False
        except Exception as e:
            print(f"[ERROR] Неожиданная ошибка отправки: {e}")
            self._release_locked()
            return False

    def _release_locked(self):
        if self.dev is not None:
            try:
                usb.util.release_interface(self.dev, USB_INTERFACE)
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
        self.dev = None

    def _worker_loop(self):
        last_send = 0.0
        while self.running:
            with self._lock:
                connected = self.dev is not None

            if not connected:
                dev = self._try_connect()
                with self._lock:
                    self.dev = dev
                    connected = self.dev is not None
                self._notify_status(connected)
                if not connected:
                    time.sleep(RECONNECT_INTERVAL)
                    continue
                last_send = 0.0

            now = time.monotonic()
            with self._lock:
                is_animated = EFFECTS[self.current_effect]["animated"]
            interval = ANIMATION_INTERVAL if is_animated else HEARTBEAT_INTERVAL

            if now - last_send >= interval:
                t = now - self._start_time
                with self._lock:
                    payload = self._build_payload(t)
                    ok = self._send_locked(payload)
                    still_connected = self.dev is not None
                if ok:
                    last_send = now
                self._notify_status(still_connected)

            time.sleep(WORKER_TICK)

    def _notify_status(self, connected):
        if connected != self._last_reported_state:
            self._last_reported_state = connected
            if self.on_status_change:
                self.on_status_change(connected)

    def _start_reactive_listener(self):
        if self._reactive_listener is not None:
            return
        self.touches = []
        self._reactive_listener = ReactiveListener(self)
        self._reactive_listener.start()

    def _stop_reactive_listener(self):
        if self._reactive_listener is not None:
            self._reactive_listener.stop()
            self._reactive_listener = None
        self.touches = []

    def add_touch(self, idx):
        now = time.monotonic() - self._start_time
        with self._lock:
            self.touches.append((idx, now))
            self.touches = [
                (i, tp) for (i, tp) in self.touches if now - tp < REACTIVE_LIFETIME
            ]

    def set_effect(self, name):
        if name not in EFFECTS:
            return
        with self._lock:
            old_kind = EFFECTS[self.current_effect]["kind"]
            new_kind = EFFECTS[name]["kind"]
            self.current_effect = name
            t = time.monotonic() - self._start_time
            self._send_locked(self._build_payload(t))
        if new_kind == "reactive" and old_kind != "reactive":
            self._start_reactive_listener()
        elif new_kind != "reactive" and old_kind == "reactive":
            self._stop_reactive_listener()
        self._persist_state()

    def set_color(self, rgb):
        with self._lock:
            self.current_rgb = rgb
            t = time.monotonic() - self._start_time
            self._send_locked(self._build_payload(t))
        self._persist_state()

    def set_brightness(self, value):
        with self._lock:
            self.brightness = int(value)
            t = time.monotonic() - self._start_time
            self._send_locked(self._build_payload(t))
        self._persist_state()

    def set_speed(self, value):
        with self._lock:
            self.speed = max(0.1, value)
            t = time.monotonic() - self._start_time
            self._send_locked(self._build_payload(t))
        self._persist_state()

    def set_direction(self, direction):
        with self._lock:
            self.direction = 1 if direction >= 0 else -1
            t = time.monotonic() - self._start_time
            self._send_locked(self._build_payload(t))
        self._persist_state()

    def set_axis(self, axis):
        with self._lock:
            self.axis = "v" if axis == "v" else "h"
            t = time.monotonic() - self._start_time
            self._send_locked(self._build_payload(t))
        self._persist_state()

    def is_connected(self):
        with self._lock:
            return self.dev is not None

    def shutdown(self):
        self.running = False
        self._stop_reactive_listener()
        self._flush_state()
        with self._lock:
            if self.dev is not None:
                try:
                    blank = bytearray(PAYLOAD_SIZE)
                    blank[0] = CMD1
                    blank[1] = CMD2_CUSTOM
                    blank[6] = HEADER_B6
                    blank[7] = HEADER_B7
                    self._send_locked(blank)
                    time.sleep(0.05)
                except Exception:
                    pass
                self._release_locked()


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

PALETTE = {
    "bg":        "#121214",
    "panel":     "#19191d",
    "panel_alt": "#232329",
    "accent":    "#7c9eff",
    "accent_hi": "#9db4ff",
    "text":      "#eaeaf0",
    "subtext":   "#8b8b95",
    "dim":       "#4a4a52",
    "ok":        "#5ddc97",
    "err":       "#ff6b81",
}

DIR_RIGHT = "Вправо ▶"
DIR_LEFT  = "◀ Влево"
DIR_DOWN  = "Вниз ▼"
DIR_UP    = "▲ Вверх"
AXIS_H    = "⟷ Горизонталь"
AXIS_V    = "↕ Вертикаль"

PRESET_COLORS = [
    ("Красный",     (255, 0, 0)),
    ("Алый",        (255, 45, 45)),
    ("Оранжевый",   (255, 120, 0)),
    ("Янтарный",    (255, 176, 0)),
    ("Жёлтый",      (255, 230, 0)),
    ("Лайм",        (150, 255, 0)),
    ("Зелёный",     (0, 220, 60)),
    ("Мятный",      (0, 255, 150)),
    ("Бирюзовый",   (0, 230, 210)),
    ("Голубой",     (0, 170, 255)),
    ("Синий",       (30, 90, 255)),
    ("Индиго",      (90, 60, 255)),
    ("Фиолетовый",  (160, 30, 255)),
    ("Пурпурный",   (220, 0, 220)),
    ("Розовый",     (255, 0, 130)),
    ("Белый",       (255, 255, 255)),
]


class ColorWheel(ctk.CTkFrame):
    def __init__(self, master, size=168, on_change=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.size = size
        # диск чуть меньше общего размера — снаружи оставляем место под
        # аккуратное кольцо-обводку
        self.radius = size / 2 * 0.86
        self.on_change = on_change
        self._hue = 0.0
        self._sat = 1.0
        self._enabled = True

        self.canvas = tk.Canvas(
            self, width=size, height=size,
            highlightthickness=0, bg=PALETTE["panel"], bd=0
        )
        self.canvas.pack()

        self._wheel_photo = ImageTk.PhotoImage(self._render_wheel())
        self.canvas.create_image(size / 2, size / 2, image=self._wheel_photo)

        r = 7
        self._cursor_ring = self.canvas.create_oval(
            -r, -r, r, r, outline="#ffffff", width=2
        )
        self._cursor_dot = self.canvas.create_oval(
            -2, -2, 2, 2, fill="#11111b", outline=""
        )

        self.canvas.bind("<Button-1>", self._on_pointer)
        self.canvas.bind("<B1-Motion>", self._on_pointer)

        self._anim_job = None

    def _render_wheel(self):
        """Рисует цветовой диск + аккуратное сглаженное кольцо-обводку
        с мягким свечением. Рендерим с суперсэмплингом (4x), затем
        уменьшаем — так кольцо и диск получаются гладкими, без зубцов."""
        size = self.size
        scale = 4
        big = size * scale
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        px = img.load()
        cx = cy = big / 2.0
        r = self.radius * scale
        for y in range(big):
            dy = y - cy
            for x in range(big):
                dx = x - cx
                dist = math.hypot(dx, dy)
                if dist <= r:
                    angle = (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0
                    hue = angle / 360.0
                    sat = min(1.0, dist / r)
                    rr, gg, bb = colorsys.hsv_to_rgb(hue, sat, 1.0)
                    px[x, y] = (int(rr * 255), int(gg * 255), int(bb * 255), 255)

        draw = ImageDraw.Draw(img)
        accent = PALETTE["accent"]
        ar, ag, ab = int(accent[1:3], 16), int(accent[3:5], 16), int(accent[5:7], 16)

        # мягкое внешнее свечение — несколько всё более крупных и прозрачных колец
        glow_layers = 5
        for i in range(glow_layers, 0, -1):
            spread = i * (scale * 1.6)
            alpha = int(38 * (1 - i / (glow_layers + 1)))
            draw.ellipse(
                [cx - r - spread, cy - r - spread, cx + r + spread, cy + r + spread],
                outline=(ar, ag, ab, alpha), width=int(scale * 1.4)
            )

        # основное чёткое кольцо
        ring_w = max(2, int(scale * 1.6))
        draw.ellipse(
            [cx - r - ring_w / 2, cy - r - ring_w / 2, cx + r + ring_w / 2, cy + r + ring_w / 2],
            outline=(ar, ag, ab, 255), width=ring_w
        )
        # тонкая яркая внутренняя грань кольца для глубины
        draw.ellipse(
            [cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1],
            outline=(255, 255, 255, 90), width=max(1, scale // 3)
        )

        img = img.resize((size, size), Image.LANCZOS)
        return img

    def _move_cursor(self, hue, sat):
        cx = cy = self.size / 2.0
        ang = math.radians(hue * 360.0)
        dist = sat * self.radius
        x = cx + dist * math.cos(ang)
        y = cy + dist * math.sin(ang)
        r = 7
        self.canvas.coords(self._cursor_ring, x - r, y - r, x + r, y + r)
        self.canvas.coords(self._cursor_dot, x - 2, y - 2, x + 2, y + 2)

    def _on_pointer(self, event):
        if not self._enabled:
            return
        cx = cy = self.size / 2.0
        dx = event.x - cx
        dy = event.y - cy
        dist = min(math.hypot(dx, dy), self.radius)
        angle = (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0
        hue = angle / 360.0
        sat = dist / self.radius if self.radius else 0.0
        self.set_hs(hue, sat, notify=True)

    def set_hs(self, hue, sat, notify=False):
        self._hue, self._sat = hue, sat
        self._move_cursor(hue, sat)
        if notify and self.on_change:
            self.on_change(self.get_rgb())

    def set_rgb(self, rgb):
        r, g, b = (c / 255.0 for c in rgb)
        h, s, _v = colorsys.rgb_to_hsv(r, g, b)
        self.set_hs(h, s if s > 0 else 0.0, notify=False)

    def get_rgb(self):
        r, g, b = colorsys.hsv_to_rgb(self._hue, self._sat, 1.0)
        return (int(r * 255), int(g * 255), int(b * 255))

    def set_enabled(self, enabled):
        self._enabled = enabled
        ring_state = "normal" if enabled else "hidden"
        self.canvas.itemconfigure(self._cursor_ring, state=ring_state)
        self.canvas.itemconfigure(self._cursor_dot, state=ring_state)

    def animate_to_rgb(self, rgb, duration_ms=280, on_step=None, on_done=None):
        """Плавно 'подъезжает' курсор колеса к позиции статичного цвета."""
        if self._anim_job is not None:
            self.after_cancel(self._anim_job)
            self._anim_job = None

        start_rgb = self.get_rgb()
        start_t = time.monotonic()

        def step():
            elapsed_ms = (time.monotonic() - start_t) * 1000.0
            frac = min(1.0, elapsed_ms / duration_ms)
            eased = 1 - (1 - frac) ** 3  # ease-out
            cur_rgb = _mix(start_rgb, rgb, eased)
            r, g, b = (c / 255.0 for c in cur_rgb)
            h, s, _v = colorsys.rgb_to_hsv(r, g, b)
            self._hue, self._sat = h, (s if s > 0 else 0.0)
            self._move_cursor(self._hue, self._sat)
            if on_step:
                on_step(cur_rgb)
            if frac < 1.0:
                self._anim_job = self.after(16, step)
            else:
                self._anim_job = None
                self.set_rgb(rgb)
                if on_done:
                    on_done(self.get_rgb())

        step()


class PresetPalette(ctk.CTkFrame):
    """Ряд статичных цветов. Рисуем кружки напрямую на Canvas (а не через
    CTkButton) — у CTkButton на маленьких размерах цвет 'съедается'
    внутренними отступами/рамкой темы и выглядит выцветшим. Здесь заливка
    круга — это ровно тот hex, что передан, без подмешивания темы."""

    def __init__(self, master, colors, on_select=None, swatch=30, gap=10, cols=8, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.colors = colors
        self.on_select = on_select
        self.swatch = swatch
        self.gap = gap
        self.cols = cols

        rows = math.ceil(len(colors) / cols)
        width = cols * (swatch + gap) - gap
        height = rows * (swatch + gap) - gap

        self.canvas = tk.Canvas(
            self, width=width, height=height,
            highlightthickness=0, bg=PALETTE["panel"], bd=0
        )
        self.canvas.pack(anchor="w")

        self._items = []
        for idx, (name, rgb) in enumerate(colors):
            col = idx % cols
            row = idx // cols
            x0 = col * (swatch + gap)
            y0 = row * (swatch + gap)
            x1, y1 = x0 + swatch, y0 + swatch
            hexc = "#%02x%02x%02x" % rgb
            oval = self.canvas.create_oval(
                x0, y0, x1, y1, fill=hexc, outline=PALETTE["dim"], width=1
            )
            self._items.append({"id": oval, "rgb": rgb, "name": name, "bbox": (x0, y0, x1, y1)})

        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.configure(cursor="hand2")
        self._selected_id = None
        self._hover_id = None

    def _on_click(self, event):
        for item in self._items:
            x0, y0, x1, y1 = item["bbox"]
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._select(item)
                return

    def _on_motion(self, event):
        hovered = None
        for item in self._items:
            x0, y0, x1, y1 = item["bbox"]
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                hovered = item
                break
        hovered_id = hovered["id"] if hovered else None
        if hovered_id == self._hover_id:
            return
        if self._hover_id is not None and self._hover_id != self._selected_id:
            self.canvas.itemconfigure(self._hover_id, outline=PALETTE["dim"], width=1)
        if hovered_id is not None and hovered_id != self._selected_id:
            self.canvas.itemconfigure(hovered_id, outline=PALETTE["accent_hi"], width=2)
        self._hover_id = hovered_id

    def _on_leave(self, event):
        if self._hover_id is not None and self._hover_id != self._selected_id:
            self.canvas.itemconfigure(self._hover_id, outline=PALETTE["dim"], width=1)
        self._hover_id = None

    def _select(self, item):
        if self._selected_id is not None:
            self.canvas.itemconfigure(self._selected_id, outline=PALETTE["dim"], width=1)
        self.canvas.itemconfigure(item["id"], outline="#ffffff", width=2)
        self._selected_id = item["id"]
        if self.on_select:
            self.on_select(item["rgb"], item["name"])

    def clear_selection(self):
        if self._selected_id is not None:
            self.canvas.itemconfigure(self._selected_id, outline=PALETTE["dim"], width=1)
            self._selected_id = None


class FullWidthDropdown(ctk.CTkFrame):
    """Кнопка-выпадашка, чей список открывается на всю ширину кнопки,
    а не 'куском слева', как это делает стандартный CTkOptionMenu."""

    def __init__(self, master, values, command=None, initial=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.values = list(values)
        self.command = command
        self._current = initial if initial in self.values else (self.values[0] if self.values else "")
        self._popup = None

        self.button = ctk.CTkButton(
            self, text=self._current, command=self._toggle,
            fg_color=PALETTE["panel_alt"], hover_color=PALETTE["panel_alt"],
            text_color=PALETTE["text"], anchor="w", corner_radius=10, height=36,
        )
        self.button.pack(fill="x")

        self.bind("<Destroy>", lambda e: self._close())

    def set(self, value):
        self._current = value
        self.button.configure(text=value)

    def get(self):
        return self._current

    def _toggle(self):
        if self._popup is not None:
            self._close()
        else:
            self._open()

    def _open(self):
        self.update_idletasks()
        x = self.button.winfo_rootx()
        y = self.button.winfo_rooty() + self.button.winfo_height() + 2
        width = self.button.winfo_width()
        row_h = 34
        height = min(row_h * len(self.values) + 8, 320)

        self._popup = tk.Toplevel(self)
        self._popup.overrideredirect(True)
        self._popup.geometry(f"{width}x{height}+{x}+{y}")
        self._popup.configure(bg=PALETTE["panel_alt"])
        try:
            self._popup.attributes("-topmost", True)
        except Exception:
            pass

        outer = ctk.CTkFrame(
            self._popup, fg_color=PALETTE["panel_alt"], corner_radius=10,
            border_width=1, border_color=PALETTE["accent"]
        )
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        if height >= 300:
            list_area = ctk.CTkScrollableFrame(outer, fg_color="transparent")
            list_area.pack(fill="both", expand=True, padx=3, pady=3)
        else:
            list_area = ctk.CTkFrame(outer, fg_color="transparent")
            list_area.pack(fill="both", expand=True, padx=3, pady=3)

        for val in self.values:
            is_current = (val == self._current)
            b = ctk.CTkButton(
                list_area, text=val, anchor="w",
                fg_color=PALETTE["accent"] if is_current else "transparent",
                hover_color=PALETTE["accent_hi"],
                text_color=("#14141a" if is_current else PALETTE["text"]),
                corner_radius=8, height=32,
                command=lambda v=val: self._select(v),
            )
            b.pack(fill="x", padx=2, pady=1)

        # ВАЖНО: раньше закрытие по <FocusOut> уничтожало popup до того, как
        # успевал сработать клик по пункту списка (Toplevel с overrideredirect
        # часто теряет фокус ещё на нажатии кнопки мыши) — из-за этого меню
        # "ломалось": открывалось и тут же схлопывалось без выбора. Вместо
        # этого используем глобальное отслеживание кликов через bind_all и
        # закрываем только если клик пришёлся мимо popup и мимо самой кнопки.
        self._popup.bind("<Escape>", lambda e: self._close())
        self._popup.after(10, self._arm_outside_click)

    def _arm_outside_click(self):
        if self._popup is None:
            return
        self._popup.bind_all("<Button-1>", self._on_global_click, add="+")

    def _on_global_click(self, event):
        if self._popup is None:
            return
        w = event.widget
        if w is self.button:
            return  # переключение обрабатывает сама кнопка
        cur = w
        while cur is not None:
            if cur is self._popup:
                return  # клик внутри выпадающего списка — не закрываем
            cur = getattr(cur, "master", None)
        self._close()

    def _select(self, value):
        self.set(value)
        self._close()
        if self.command:
            self.command(value)

    def _close(self):
        if self._popup is not None:
            try:
                self._popup.unbind_all("<Button-1>")
            except Exception:
                pass
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None

    def configure(self, **kwargs):
        # совместимость с местами, где вызывается .configure(state=...)
        state = kwargs.pop("state", None)
        if state is not None:
            self.button.configure(state=state)
        if kwargs:
            super().configure(**kwargs)


class App(ctk.CTk):
    def __init__(self, controller: LeobogController):
        super().__init__()
        self.controller = controller
        self.controller.on_status_change = self._on_status_change
        self.quit_callback = None
        self.tray_available = False
        self._rgb_editing = False

        self.title("LEOBOG HI75C Studio")
        self.geometry("900x540")
        self.minsize(900, 540)
        self.resizable(False, False)
        self.configure(fg_color=PALETTE["bg"])
        self.protocol("WM_DELETE_WINDOW", self.hide_window)

        self._build_ui()
        self._on_status_change(self.controller.is_connected())
        self._start_preview_loop()

        # Гарантированный источник "нажатий" для режима "Реакция на нажатие":
        # пока системный evdev-слушатель может упираться в права доступа,
        # этот способ работает всегда, пока окно приложения в фокусе —
        # так эффект точно можно проверить и увидеть вживую.
        self.bind_all("<KeyPress>", self._on_local_keypress, add="+")

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(22, 4))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(side="left")
        self.title_icon = ctk.CTkLabel(
            title_row, text="⌨", font=ctk.CTkFont(size=26),
            text_color=PALETTE["accent"], width=34
        )
        self.title_icon.pack(side="left", padx=(0, 10))
        title_box = ctk.CTkFrame(title_row, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(
            title_box, text="LEOBOG HI75C",
            font=ctk.CTkFont(size=21, weight="bold"), text_color=PALETTE["text"]
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box, text="RGB Studio",
            font=ctk.CTkFont(size=11), text_color=PALETTE["dim"]
        ).pack(anchor="w")

        self.status_chip = ctk.CTkFrame(
            header, fg_color=PALETTE["panel"], corner_radius=999,
            border_width=1, border_color=PALETTE["err"]
        )
        self.status_chip.pack(side="right", pady=(2, 0))
        self.status_dot = ctk.CTkLabel(
            self.status_chip, text="●", font=ctk.CTkFont(size=12), text_color=PALETTE["err"]
        )
        self.status_dot.pack(side="left", padx=(14, 6), pady=7)
        self.status_lbl = ctk.CTkLabel(
            self.status_chip, text="Офлайн", font=ctk.CTkFont(size=12, weight="bold"),
            text_color=PALETTE["subtext"]
        )
        self.status_lbl.pack(side="left", padx=(0, 14), pady=7)

        divider = ctk.CTkFrame(self, fg_color=PALETTE["panel"], height=1)
        divider.pack(fill="x", padx=24, pady=(14, 12))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        body.grid_columnconfigure(0, weight=1, uniform="col")
        body.grid_columnconfigure(1, weight=1, uniform="col")
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(
            body, corner_radius=18, fg_color=PALETTE["panel"],
            border_width=1, border_color=PALETTE["panel_alt"]
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ctk.CTkFrame(
            body, corner_radius=18, fg_color=PALETTE["panel"],
            border_width=1, border_color=PALETTE["panel_alt"]
        )
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self._label(left, "РЕЖИМ").pack(anchor="w", padx=18, pady=(18, 6))
        self.effect_menu = FullWidthDropdown(
            left, values=list(EFFECTS.keys()), command=self._on_effect_change,
            initial=self.controller.current_effect,
        )
        self.effect_menu.pack(fill="x", padx=18, pady=(0, 16))

        self.axis_title = self._label(left, "ОСЬ")
        self.axis_title.pack(anchor="w", padx=18, pady=(0, 6))
        self.axis_seg = ctk.CTkSegmentedButton(
            left, values=[AXIS_H, AXIS_V], command=self._on_axis_change,
            fg_color=PALETTE["panel_alt"], selected_color=PALETTE["accent"],
            selected_hover_color=PALETTE["accent_hi"], unselected_color=PALETTE["panel_alt"],
            text_color=PALETTE["text"], corner_radius=10, height=32,
        )
        # Ось/направление/скорость восстанавливаются из сохранённого
        # состояния (последний выбранный режим), а не сбрасываются на
        # значения по умолчанию при каждом запуске.
        init_axis = "v" if self.controller.axis == "v" else "h"
        init_sign = 1 if self.controller.direction >= 0 else -1
        self.axis_seg.set(AXIS_V if init_axis == "v" else AXIS_H)
        self.axis_seg.pack(fill="x", padx=18, pady=(0, 14))

        self.dir_title = self._label(left, "НАПРАВЛЕНИЕ")
        self.dir_title.pack(anchor="w", padx=18, pady=(0, 6))
        init_dir_labels = (DIR_DOWN, DIR_UP) if init_axis == "v" else (DIR_RIGHT, DIR_LEFT)
        self.direction_seg = ctk.CTkSegmentedButton(
            left, values=list(init_dir_labels), command=self._on_direction_change,
            fg_color=PALETTE["panel_alt"], selected_color=PALETTE["accent"],
            selected_hover_color=PALETTE["accent_hi"], unselected_color=PALETTE["panel_alt"],
            text_color=PALETTE["text"], corner_radius=10, height=32,
        )
        self.direction_seg.set(init_dir_labels[0] if init_sign >= 0 else init_dir_labels[1])
        self.direction_seg.pack(fill="x", padx=18, pady=(0, 18))

        self._label(left, "ЯРКОСТЬ").pack(anchor="w", padx=18, pady=(0, 4))
        brow = ctk.CTkFrame(left, fg_color="transparent")
        brow.pack(fill="x", padx=18)
        self.brightness_slider = ctk.CTkSlider(
            brow, from_=0, to=100, command=self._on_brightness_change,
            progress_color=PALETTE["accent"], button_color=PALETTE["accent"],
            button_hover_color=PALETTE["accent_hi"],
        )
        self.brightness_slider.set(self.controller.brightness)
        self.brightness_slider.pack(side="left", fill="x", expand=True)
        self.brightness_val_lbl = ctk.CTkLabel(
            brow, text=f"{self.controller.brightness}%", width=42,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=PALETTE["text"]
        )
        self.brightness_val_lbl.pack(side="left", padx=(10, 0))

        self._label(left, "СКОРОСТЬ").pack(anchor="w", padx=18, pady=(14, 4))
        srow = ctk.CTkFrame(left, fg_color="transparent")
        srow.pack(fill="x", padx=18, pady=(0, 18))
        self.speed_slider = ctk.CTkSlider(
            srow, from_=25, to=400, command=self._on_speed_change,
            progress_color=PALETTE["accent"], button_color=PALETTE["accent"],
            button_hover_color=PALETTE["accent_hi"],
        )
        init_speed_pct = max(25, min(400, round(self.controller.speed * 100)))
        self.speed_slider.set(init_speed_pct)
        self.speed_slider.pack(side="left", fill="x", expand=True)
        self.speed_val_lbl = ctk.CTkLabel(
            srow, text=f"{init_speed_pct}%", width=42,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=PALETTE["text"]
        )
        self.speed_val_lbl.pack(side="left", padx=(10, 0))

        self._label(right, "ЦВЕТ").pack(anchor="w", padx=18, pady=(18, 6))

        color_row = ctk.CTkFrame(right, fg_color="transparent")
        color_row.pack(padx=18, pady=(4, 10), fill="x")

        wheel_col = ctk.CTkFrame(color_row, fg_color="transparent")
        wheel_col.pack(side="left")
        self.color_wheel = ColorWheel(wheel_col, size=148, on_change=self._on_wheel_change)
        self.color_wheel.set_rgb(self.controller.current_rgb)
        self.color_wheel.pack()

        rgb_col = ctk.CTkFrame(color_row, fg_color="transparent")
        rgb_col.pack(side="left", fill="both", expand=True, padx=(18, 0))

        self._label(rgb_col, "RGB").pack(anchor="w", pady=(0, 6))
        self.rgb_entries = {}
        for ch in ("R", "G", "B"):
            erow = ctk.CTkFrame(rgb_col, fg_color="transparent")
            erow.pack(fill="x", pady=4)
            ctk.CTkLabel(
                erow, text=ch, width=16, font=ctk.CTkFont(size=12, weight="bold"),
                text_color=PALETTE["subtext"]
            ).pack(side="left")
            entry = ctk.CTkEntry(
                erow, width=64, justify="center",
                fg_color=PALETTE["panel_alt"], text_color=PALETTE["text"],
                border_width=1, border_color=PALETTE["panel_alt"], corner_radius=8,
            )
            entry.insert(0, str(self.controller.current_rgb["RGB".index(ch)]))
            entry.bind("<Return>", self._on_rgb_entry_commit)
            entry.bind("<KP_Enter>", self._on_rgb_entry_commit)
            entry.bind("<FocusIn>", lambda e, en=entry: self._on_rgb_focus_in(en))
            entry.bind("<FocusOut>", self._on_rgb_entry_commit)
            entry.pack(side="left", padx=(8, 0))
            self.rgb_entries[ch] = entry

        ctk.CTkButton(
            rgb_col, text="Применить", command=self._on_rgb_entry_commit,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent_hi"],
            text_color="#14141a", corner_radius=8, height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(fill="x", pady=(10, 0))

        hex_row = ctk.CTkFrame(right, fg_color="transparent")
        hex_row.pack(pady=(0, 10))
        self.hex_swatch = ctk.CTkFrame(
            hex_row, width=18, height=18, corner_radius=5,
            fg_color="#ffffff", border_width=1, border_color=PALETTE["dim"]
        )
        self.hex_swatch.pack(side="left", padx=(0, 8))
        self.hex_swatch.pack_propagate(False)
        self.color_hex_lbl = ctk.CTkLabel(
            hex_row, text="", font=ctk.CTkFont(size=13, weight="bold"),
            text_color=PALETTE["subtext"]
        )
        self.color_hex_lbl.pack(side="left")

        self._label(right, "СТАНДАРТНЫЕ ЦВЕТА").pack(anchor="w", padx=18, pady=(0, 6))
        self.preset_palette = PresetPalette(
            right, PRESET_COLORS, on_select=self._on_preset_click,
            swatch=28, gap=8, cols=8,
        )
        self.preset_palette.pack(padx=18, pady=(0, 12), anchor="w")

        self.tray_hint_lbl = ctk.CTkLabel(
            right, text="", font=ctk.CTkFont(size=10), text_color=PALETTE["dim"],
            wraplength=280, justify="left"
        )
        self.tray_hint_lbl.pack(side="bottom", padx=18, pady=(0, 6))

        ctk.CTkLabel(
            right, text="Все эффекты рендерятся на хосте",
            font=ctk.CTkFont(size=10), text_color=PALETTE["dim"]
        ).pack(side="bottom", pady=(0, 4))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", padx=24, pady=(0, 20), fill="x")
        ctk.CTkButton(
            btn_row, text="🗕  Свернуть в трей", command=self.hide_window,
            fg_color=PALETTE["panel_alt"], hover_color=PALETTE["dim"],
            text_color=PALETTE["text"], corner_radius=12, height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            border_width=1, border_color=PALETTE["panel_alt"],
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="✕  Выход", command=self._quit,
            fg_color=PALETTE["err"], hover_color="#e0687d",
            text_color="#14141a", corner_radius=12, height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

        self._update_ui_state()

    def _label(self, parent, text):
        return ctk.CTkLabel(
            parent, text=text, font=ctk.CTkFont(size=10, weight="bold"),
            text_color=PALETTE["subtext"]
        )

    def _start_preview_loop(self):
        self._update_color_preview()
        self.after(80, self._start_preview_loop)

    def _update_color_preview(self):
        r, g, b = self.controller.current_rgb
        factor = self.controller.brightness / 100.0
        rr, gg, bb = (int(c * factor) for c in (r, g, b))
        hex_c = f"#{rr:02x}{gg:02x}{bb:02x}"
        self.color_hex_lbl.configure(text=hex_c.upper())
        if hasattr(self, "hex_swatch"):
            self.hex_swatch.configure(fg_color=hex_c)
        self._sync_rgb_entries(self.controller.current_rgb)

    def _sync_rgb_entries(self, rgb):
        if self._rgb_editing:
            return  # пользователь сейчас печатает в одно из полей — не мешаем
        for ch, val in zip(("R", "G", "B"), rgb):
            entry = self.rgb_entries.get(ch)
            if entry is None:
                continue
            text = str(int(val))
            if entry.get() != text:
                entry.delete(0, "end")
                entry.insert(0, text)

    def _on_rgb_focus_in(self, entry=None):
        self._rgb_editing = True
        if entry is not None:
            entry.configure(border_color=PALETTE["accent"])

    def _update_ui_state(self):
        cfg = EFFECTS[self.controller.current_effect]
        uses_color = cfg["uses_color"]
        directional = cfg["directional"]
        has_axis = cfg["has_axis"]

        self.color_wheel.set_enabled(uses_color)
        if uses_color:
            self.color_hex_lbl.configure(text_color=PALETTE["subtext"])
            self._update_color_preview()
        else:
            self.color_hex_lbl.configure(
                text="ЦВЕТ ЗАДАЁТСЯ АВТОМАТИЧЕСКИ", text_color=PALETTE["dim"]
            )

        self.axis_seg.configure(state="normal" if has_axis else "disabled")
        self.axis_title.configure(
            text_color=PALETTE["subtext"] if has_axis else PALETTE["dim"]
        )
        self.direction_seg.configure(state="normal" if directional else "disabled")
        self.dir_title.configure(
            text_color=PALETTE["subtext"] if directional else PALETTE["dim"]
        )

    def _on_status_change(self, connected):
        self.after(0, lambda: self._apply_status(connected))

    def _on_local_keypress(self, event):
        # Гарантированный триггер реактивного эффекта, пока окно программы
        # в фокусе — не зависит от evdev/прав доступа, работает всегда.
        if EFFECTS[self.controller.current_effect]["kind"] != "reactive":
            return
        self.controller.add_touch(_tk_event_to_idx(event))

    def _apply_status(self, connected):
        if connected:
            self.status_dot.configure(text_color=PALETTE["ok"])
            self.status_lbl.configure(text="Подключено", text_color=PALETTE["text"])
            self.status_chip.configure(border_color=PALETTE["ok"])
        else:
            self.status_dot.configure(text_color=PALETTE["err"])
            self.status_lbl.configure(text="Поиск устройства…", text_color=PALETTE["subtext"])
            self.status_chip.configure(border_color=PALETTE["err"])

    def _on_effect_change(self, name):
        self.controller.set_effect(name)
        self._update_ui_state()

    def _on_wheel_change(self, rgb):
        self.controller.set_color(rgb)
        self.preset_palette.clear_selection()
        self._update_color_preview()

    def _on_preset_click(self, rgb, name):
        # колесо само "уезжает" (курсор плавно скользит) на статичный цвет
        def on_step(cur_rgb):
            self.controller.set_color(cur_rgb)
            self._update_color_preview()

        def on_done(final_rgb):
            self.controller.set_color(final_rgb)
            self._update_color_preview()

        self.color_wheel.animate_to_rgb(rgb, duration_ms=280, on_step=on_step, on_done=on_done)

    def _on_rgb_entry_commit(self, event=None):
        self._rgb_editing = False
        for entry in self.rgb_entries.values():
            entry.configure(border_color=PALETTE["panel_alt"])
        try:
            r = int(self.rgb_entries["R"].get())
            g = int(self.rgb_entries["G"].get())
            b = int(self.rgb_entries["B"].get())
        except ValueError:
            self._sync_rgb_entries(self.controller.current_rgb)
            return
        r, g, b = (max(0, min(255, v)) for v in (r, g, b))
        rgb = (r, g, b)
        self.controller.set_color(rgb)
        self.color_wheel.set_rgb(rgb)
        self.preset_palette.clear_selection()
        self._update_color_preview()

    def _on_axis_change(self, value):
        axis = "v" if value == AXIS_V else "h"
        self.controller.set_axis(axis)
        sign = 1 if self.controller.direction >= 0 else -1
        labels = (DIR_DOWN, DIR_UP) if axis == "v" else (DIR_RIGHT, DIR_LEFT)
        self.direction_seg.configure(values=list(labels))
        self.direction_seg.set(labels[0] if sign >= 0 else labels[1])

    def _on_direction_change(self, value):
        sign = 1 if value in (DIR_RIGHT, DIR_DOWN) else -1
        self.controller.set_direction(sign)

    def _on_brightness_change(self, val):
        self.controller.set_brightness(float(val))
        self.brightness_val_lbl.configure(text=f"{int(float(val))}%")
        self._update_color_preview()

    def _on_speed_change(self, val):
        pct = float(val)
        self.controller.set_speed(pct / 100.0)
        self.speed_val_lbl.configure(text=f"{int(pct)}%")

    def set_tray_status(self, available):
        self.tray_available = available
        if available:
            self.tray_hint_lbl.configure(text="")
        else:
            self.tray_hint_lbl.configure(
                text="Иконка трея недоступна в этом окружении (нужен SNI-хост, "
                     "например модуль трея в waybar). Кнопка «Свернуть» сворачивает "
                     "окно на панель задач."
            )

    def hide_window(self):
        if self.tray_available:
            self.withdraw()
        else:
            self.iconify()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit(self):
        if self.quit_callback:
            self.quit_callback()
        else:
            self.controller.shutdown()
            self.destroy()
            sys.exit(0)


LOCK_PATH = os.path.join(tempfile.gettempdir(), "leobog_hi75c_studio.lock")


def _read_lock_pid():
    try:
        with open(LOCK_PATH, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _write_lock():
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"[ЗАПУСК] Не удалось создать lock-файл: {e}")


def _remove_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def _check_single_instance(quiet=False):
    """Если приложение уже запущено (в трее или окном) — предупредить и
    предложить закрыть старый процесс и перезапуститься, либо отменить запуск.

    При автозапуске (quiet=True) экран с вопросом не нужен: это, скорее
    всего, повторный автозапуск при быстром релогине, поэтому новый
    процесс просто тихо завершается, не мешая уже работающему."""
    pid = _read_lock_pid()
    if not _pid_alive(pid):
        return  # процесса нет — lock устарел, спокойно продолжаем

    if quiet:
        print(f"[ЗАПУСК] Уже работает (PID {pid}) — автозапуск пропущен.")
        sys.exit(0)

    root = tk.Tk()
    root.withdraw()
    try:
        restart = messagebox.askyesno(
            "LEOBOG HI75C Studio",
            f"Приложение уже запущено (PID {pid}), скорее всего свёрнуто в трей.\n\n"
            "Закрыть предыдущий процесс и запустить заново?\n\n"
            "«Да» — закрыть старый и перезапустить.\n"
            "«Нет» — отменить этот запуск.",
        )
    finally:
        root.destroy()

    if not restart:
        print("[ЗАПУСК] Отменено пользователем: приложение уже работает.")
        sys.exit(0)

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(30):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    _remove_lock()


def create_tray_image():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    dc.rounded_rectangle([8, 8, 56, 56], radius=14, fill=(25, 25, 29, 255),
                          outline=(124, 158, 255, 255), width=3)
    dc.ellipse([22, 22, 42, 42], fill=(124, 158, 255, 255))
    return image


def _parse_args():
    parser = argparse.ArgumentParser(description="LEOBOG HI75C Studio")
    parser.add_argument(
        "--tray", "--autostart", "--minimized",
        dest="start_in_tray", action="store_true",
        help="Запуститься сразу свёрнутым в трей, не открывая окно "
             "настроек. Для автозапуска вместе с Hyprland/DE: "
             "exec-once = /путь/до/папки/run.sh --tray",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    _check_single_instance(quiet=args.start_in_tray)
    _write_lock()
    atexit.register(_remove_lock)

    controller = LeobogController()
    app = App(controller)

    if args.start_in_tray:
        # Не показываем окно вовсе — свет уже включится с последним
        # сохранённым режимом/цветом сам, без открытия GUI на каждый вход.
        app.withdraw()

    tray_holder = {"icon": None}

    def quit_app():
        controller.shutdown()
        _remove_lock()
        icon = tray_holder.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        try:
            app.quit()
        except Exception:
            pass
        os._exit(0)

    app.quit_callback = quit_app

    def _on_signal(signum, frame):
        quit_app()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def open_gui(icon, item):
        app.after(0, app.show_window)

    def on_tray_exit(icon, item):
        quit_app()

    tray_icon = pystray.Icon(
        "leobog_studio",
        create_tray_image(),
        "LEOBOG HI75C Studio",
        menu=pystray.Menu(
            pystray.MenuItem("Открыть настройки", open_gui, default=True),
            pystray.MenuItem("Выход", on_tray_exit),
        ),
    )
    tray_holder["icon"] = tray_icon

    backend = os.environ.get("PYSTRAY_BACKEND", "auto (xorg/gtk)")
    print(f"[ТРЕЙ] Бэкенд pystray: {backend}")
    if backend == "auto (xorg/gtk)":
        print(
            "[ТРЕЙ] AppIndicator-бэкенд не найден — на Wayland/Hyprland трей, "
            "скорее всего, не появится. Поставь системные пакеты (см. README) "
            "и убедись, что в баре (waybar/ironbar/eww) включён модуль трея."
        )

    def _run_tray():
        try:
            tray_icon.run()
        except Exception as e:
            print(
                f"[ТРЕЙ] Системная иконка недоступна: {e}\n"
                "[ТРЕЙ] На Hyprland/Wayland трею нужен SNI-хост (например, модуль "
                "трея в waybar) и пакет libayatana-appindicator3-1. Пока пользуйтесь "
                "кнопкой «Выход» и «Свернуть в трей» прямо в окне."
            )

    threading.Thread(target=_run_tray, daemon=True).start()

    def _check_tray_ready():
        ready = False
        try:
            ready = bool(tray_icon.visible)
        except Exception:
            ready = False
        app.set_tray_status(ready)
        if not ready:
            app.after(1500, _check_tray_ready)

    app.after(1000, _check_tray_ready)

    app.mainloop()


if __name__ == "__main__":
    main()
