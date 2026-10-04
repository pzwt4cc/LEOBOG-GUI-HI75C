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

import argparse
import atexit
import colorsys
import errno as _errno
import fcntl
import glob
import json
import math
import os
import signal
import sys
import tempfile
import threading
import time
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

VENDOR_ID = 0x258A
PRODUCT_ID = 0x010C
USB_INTERFACE = 1
PAYLOAD_SIZE = 520
TOTAL_LED_SLOTS = 170

CMD1 = 0x06
CMD2_CUSTOM = 0x08
HEADER_B6 = 0x7A
HEADER_B7 = 0x01

EFFECTS = {
    "🎨 Сплошной цвет": {
        "kind": "solid",
        "uses_color": True,
        "animated": False,
        "directional": False,
        "has_axis": False,
    },
    "🌬️ Дыхание": {
        "kind": "breathe",
        "uses_color": True,
        "animated": True,
        "directional": False,
        "has_axis": False,
    },
    "🌈 Радуга": {
        "kind": "rainbow",
        "uses_color": False,
        "animated": True,
        "directional": True,
        "has_axis": True,
    },
    "🎆 Цветопереход": {
        "kind": "colorcycle",
        "uses_color": False,
        "animated": True,
        "directional": True,
        "has_axis": False,
    },
    "🌊 Волна": {
        "kind": "wave",
        "uses_color": True,
        "animated": True,
        "directional": True,
        "has_axis": True,
    },
    "🐍 Змейка": {
        "kind": "snake",
        "uses_color": True,
        "animated": True,
        "directional": True,
        "has_axis": True,
    },
    "🐍🐍 Двойная змейка": {
        "kind": "dual_snake",
        "uses_color": True,
        "animated": True,
        "directional": True,
        "has_axis": False,
    },
    "☄️ Комета": {
        "kind": "comet",
        "uses_color": True,
        "animated": True,
        "directional": True,
        "has_axis": True,
    },
    "💫 Рябь": {
        "kind": "ripple",
        "uses_color": True,
        "animated": True,
        "directional": True,
        "has_axis": True,
    },
    "✨ Мерцание": {
        "kind": "twinkle",
        "uses_color": True,
        "animated": True,
        "directional": False,
        "has_axis": False,
    },
    "👆 Реакция на нажатие": {
        "kind": "reactive",
        "uses_color": True,
        "animated": True,
        "directional": False,
        "has_axis": False,
    },
    "⭕ Кольцо от нажатия": {
        "kind": "reactive_ring",
        "uses_color": True,
        "animated": True,
        "directional": False,
        "has_axis": False,
    },
    "↔️ Ряд в обе стороны": {
        "kind": "reactive_row_both",
        "uses_color": True,
        "animated": True,
        "directional": False,
        "has_axis": False,
    },
}


def _is_reactive_kind(kind):
    """Режимы, которые зависят от нажатий клавиш."""
    return kind.startswith("reactive")


DEFAULT_EFFECT = "🎨 Сплошной цвет"

# --- Сохранение последнего выбранного режима/цвета между запусками ---
STATE_DIR = os.path.join(os.path.expanduser("~"), ".config", "leobog-hi75c-studio")
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
ANIMATION_FPS = 24
ANIMATION_INTERVAL = 1.0 / ANIMATION_FPS
RECONNECT_INTERVAL = 2.0
WORKER_TICK = 0.02

BREATH_SPEED = 1.4
RAINBOW_SPEED = 0.12
COLORCYCLE_SPEED = 0.10
WAVE_SPEED = 0.55
SNAKE_SPEED = 32.0  # клавиш в секунду
SNAKE_LEN = 12
SNAKE_V_SPEED = 2.4
SNAKE_V_TRAIL = 3
COMET_SPEED = 13.0  # колонок в секунду
COMET_LEN = 7
COMET_V_SPEED = 5.0
COMET_V_TRAIL = 3
RIPPLE_SPEED = 0.9
RIPPLE_SPREAD = 1.6
TWINKLE_HZ = 6.0
REACTIVE_SPEED = 48.0  # колонок в секунду — быстро, "пронеслось"
REACTIVE_LIFETIME = 0.65
REACTIVE_REACH = 12.0  # на каком расстоянии свет гаснет совсем
REACTIVE_EDGE = 1.5  # мягкость переднего края
RING_SPEED = 26.0  # колонок в секунду
RING_WIDTH = 1.7  # толщина кольца (чем меньше — тем тоньше)
RING_LIFETIME = 1.5  # верхний предел; кольцо гаснет само, дойдя до края
REACTIVE_ROW_REACH = 16.0  # для режимов "по ряду": почти на весь ряд

GRID_ROWS = 6
GRID_COLS = math.ceil(TOTAL_LED_SLOTS / GRID_ROWS)


def _grid_row(i):
    return min(i // GRID_COLS, GRID_ROWS - 1)


ROW_OF = [_grid_row(i) for i in range(TOTAL_LED_SLOTS)]
COL_OF = [i - ROW_OF[i] * GRID_COLS for i in range(TOTAL_LED_SLOTS)]
ROW_INDEX_LIST = [
    [i for i in range(TOTAL_LED_SLOTS) if ROW_OF[i] == r] for r in range(GRID_ROWS)
]


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
    [
        "esc",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "f11",
        "f12",
        "delete",
    ],
    [
        "grave",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "0",
        "minus",
        "equal",
        "backspace",
    ],
    [
        "tab",
        "q",
        "w",
        "e",
        "r",
        "t",
        "y",
        "u",
        "i",
        "o",
        "p",
        "bracketleft",
        "bracketright",
        "backslash",
    ],
    [
        "capslock",
        "a",
        "s",
        "d",
        "f",
        "g",
        "h",
        "j",
        "k",
        "l",
        "semicolon",
        "apostrophe",
        "enter",
    ],
    [
        "shift_l",
        "z",
        "x",
        "c",
        "v",
        "b",
        "n",
        "m",
        "comma",
        "period",
        "slash",
        "shift_r",
        "up",
    ],
    ["ctrl_l", "meta_l", "alt_l", "space", "alt_r", "ctrl_r", "left", "down", "right"],
]

KEY_GRID_POS = {}
for _row_i, _row_keys in enumerate(KEY_ROWS):
    _width = len(_row_keys)
    for _col_i, _key in enumerate(_row_keys):
        KEY_GRID_POS[_key] = (_row_i, _col_i / max(1, _width - 1))


# --- Карта клавиш LEOBOG HI75C (встроена в программу) ---
# Светодиоды идут по колонкам: номер = колонка * LED_ROWS + ряд
# (ряд 0 — F-ряд, ряд 5 — нижний). Карта снята с реальной клавиатуры
# калибровкой и подходит всем, у кого такая же модель — калибровать
# ничего не нужно. Если у кого-то раскладка другая: ./run.sh --calibrate
# сохранит свою карту в ~/.config/leobog-hi75c-studio/keymap.json, и она
# заменит встроенную.
LED_ROWS = 6
DEFAULT_KEYMAP = {
    "esc": 0,
    "grave": 1,
    "tab": 2,
    "capslock": 3,
    "shift_l": 4,
    "ctrl_l": 5,
    "1": 7,
    "q": 8,
    "a": 9,
    "z": 10,
    "meta_l": 11,
    "f1": 12,
    "2": 13,
    "w": 14,
    "s": 15,
    "x": 16,
    "alt_l": 17,
    "f2": 18,
    "3": 19,
    "e": 20,
    "d": 21,
    "c": 22,
    "f3": 24,
    "4": 25,
    "r": 26,
    "f": 27,
    "v": 28,
    "f4": 30,
    "5": 31,
    "t": 32,
    "g": 33,
    "b": 34,
    "space": 35,
    "f5": 36,
    "6": 37,
    "y": 38,
    "h": 39,
    "n": 40,
    "f6": 42,
    "7": 43,
    "u": 44,
    "j": 45,
    "m": 46,
    "f7": 48,
    "8": 49,
    "i": 50,
    "k": 51,
    "comma": 52,
    "alt_r": 53,
    "f8": 54,
    "9": 55,
    "o": 56,
    "l": 57,
    "period": 58,
    "f9": 60,
    "0": 61,
    "p": 62,
    "semicolon": 63,
    "slash": 64,
    "ctrl_r": 65,
    "f10": 66,
    "minus": 67,
    "bracketleft": 68,
    "apostrophe": 69,
    "shift_r": 70,
    "f11": 72,
    "equal": 73,
    "bracketright": 74,
    "left": 77,
    "f12": 78,
    "backspace": 79,
    "backslash": 80,
    "enter": 81,
    "up": 82,
    "down": 83,
    "delete": 85,
    "end": 86,
    "pageup": 87,
    "pagedown": 88,
    "right": 89,
}

# Для рисования клавиатуры в окне: подписи и "растянутые" клавиши
# (колонки от..до включительно). Остальные клавиши занимают одну колонку.
KEY_SPANS = {"enter": (12, 13), "shift_r": (11, 12), "space": (3, 7)}
KEY_LABELS = {
    "esc": "Esc",
    "grave": "`",
    "tab": "Tab",
    "capslock": "Caps",
    "shift_l": "Shift",
    "shift_r": "Shift",
    "ctrl_l": "Ctrl",
    "ctrl_r": "Ctrl",
    "meta_l": "Win",
    "alt_l": "Alt",
    "alt_r": "Alt",
    "space": "",
    "minus": "-",
    "equal": "=",
    "bracketleft": "[",
    "bracketright": "]",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backspace": "⌫",
    "enter": "Enter",
    "delete": "Del",
    "end": "End",
    "pageup": "PgUp",
    "pagedown": "PgDn",
    "up": "▲",
    "down": "▼",
    "left": "◀",
    "right": "▶",
}

KEYMAP_PATH = os.path.join(STATE_DIR, "keymap.json")


def _load_keymap():
    km = dict(DEFAULT_KEYMAP)
    try:
        with open(KEYMAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if isinstance(k, str) and 0 <= int(v) < TOTAL_LED_SLOTS:
                km[k] = int(v)
    except Exception:
        pass
    return km


KEYMAP = _load_keymap()

# Координаты каждого светодиода — прямо из матрицы (колонка, ряд).
# Лишние светодиоды правее последней колонки с клавишами не используются.
LED_XY = [None] * TOTAL_LED_SLOTS
_max_col = max(_i // LED_ROWS for _i in KEYMAP.values())
for _i in range(TOTAL_LED_SLOTS):
    if _i // LED_ROWS <= _max_col:
        LED_XY[_i] = (float(_i // LED_ROWS), float(_i % LED_ROWS))

# --- Геометрия для позиционных эффектов (по реальным клавишам) ---
KEY_LEDS = sorted(set(KEYMAP.values()))  # светодиоды, под которыми есть клавиши
KEY_LED_SET = set(KEY_LEDS)
N_COLS = _max_col + 1
_CX = (N_COLS - 1) / 2.0
_CY = (LED_ROWS - 1) / 2.0
# положение 0..1 вдоль горизонтали / вертикали
POS_H = [
    LED_XY[i][0] / (N_COLS - 1) if LED_XY[i] else 0.0 for i in range(TOTAL_LED_SLOTS)
]
POS_V = [
    LED_XY[i][1] / (LED_ROWS - 1) if LED_XY[i] else 0.0 for i in range(TOTAL_LED_SLOTS)
]
# расстояние до центра клавиатуры по горизонтали / вертикали, 0..1
DIST_H = [
    abs(LED_XY[i][0] - _CX) / _CX if LED_XY[i] else 0.0 for i in range(TOTAL_LED_SLOTS)
]
DIST_V = [
    abs(LED_XY[i][1] - _CY) / _CY if LED_XY[i] else 0.0 for i in range(TOTAL_LED_SLOTS)
]


def _zigzag(by_rows):
    """Путь "змейкой" по клавишам: ряд за рядом (туда-обратно) или колонка за колонкой."""
    path = []
    if by_rows:
        for r in range(LED_ROWS):
            row = sorted(
                (i for i in KEY_LEDS if i % LED_ROWS == r), key=lambda i: i // LED_ROWS
            )
            path += row if r % 2 == 0 else row[::-1]
    else:
        for c in range(N_COLS):
            col = sorted(
                (i for i in KEY_LEDS if i // LED_ROWS == c), key=lambda i: i % LED_ROWS
            )
            path += col if c % 2 == 0 else col[::-1]
    return path


PATH_H = _zigzag(True)  # змейка идёт вдоль рядов
PATH_V = _zigzag(False)  # змейка идёт вдоль колонок


_FAR_CACHE = {}


def _far_dist(origin):
    """Расстояние от светодиода до самого дальнего светодиода клавиатуры."""
    d = _FAR_CACHE.get(origin)
    if d is None:
        ox, oy = LED_XY[origin]
        d = max(math.hypot(xy[0] - ox, xy[1] - oy) for xy in LED_XY if xy is not None)
        _FAR_CACHE[origin] = d
    return d


def _logical_key_to_idx(logical):
    """Логическое имя клавиши -> индекс LED-слота."""
    if KEYMAP:
        return KEYMAP.get(logical)
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
            "ESC": "esc",
            "DELETE": "delete",
            "GRAVE": "grave",
            "MINUS": "minus",
            "EQUAL": "equal",
            "BACKSPACE": "backspace",
            "TAB": "tab",
            "LEFTBRACE": "bracketleft",
            "RIGHTBRACE": "bracketright",
            "BACKSLASH": "backslash",
            "CAPSLOCK": "capslock",
            "SEMICOLON": "semicolon",
            "APOSTROPHE": "apostrophe",
            "ENTER": "enter",
            "LEFTSHIFT": "shift_l",
            "RIGHTSHIFT": "shift_r",
            "COMMA": "comma",
            "DOT": "period",
            "SLASH": "slash",
            "UP": "up",
            "LEFTCTRL": "ctrl_l",
            "RIGHTCTRL": "ctrl_r",
            "LEFTALT": "alt_l",
            "RIGHTALT": "alt_r",
            "LEFTMETA": "meta_l",
            "RIGHTMETA": "meta_l",
            "SPACE": "space",
            "LEFT": "left",
            "DOWN": "down",
            "RIGHT": "right",
            "END": "end",
            "PAGEUP": "pageup",
            "PAGEDOWN": "pagedown",
            "HOME": "home",
            "INSERT": "insert",
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
    return None  # незнакомая клавиша (громкость и т.п.) — не светим наугад


_TK_KEYSYM_TO_LOGICAL = {
    "Escape": "esc",
    "Delete": "delete",
    "grave": "grave",
    "minus": "minus",
    "equal": "equal",
    "BackSpace": "backspace",
    "Tab": "tab",
    "bracketleft": "bracketleft",
    "bracketright": "bracketright",
    "backslash": "backslash",
    "Caps_Lock": "capslock",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "Return": "enter",
    "KP_Enter": "enter",
    "Shift_L": "shift_l",
    "Shift_R": "shift_r",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "Control_L": "ctrl_l",
    "Control_R": "ctrl_r",
    "Alt_L": "alt_l",
    "Alt_R": "alt_r",
    "Super_L": "meta_l",
    "Super_R": "meta_l",
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
    return None  # неизвестная клавиша — не светим наугад


def _scaled(rgb, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def _mix(rgb_a, rgb_b, ratio):
    return tuple(int(a + (b - a) * ratio) for a, b in zip(rgb_a, rgb_b))


def _hash01(n):
    x = math.sin(n * 12.9898) * 43758.5453
    return x - math.floor(x)


def render_frame(
    effect_name,
    base_rgb,
    brightness_pct,
    t,
    direction=1,
    axis="h",
    speed=1.0,
    touches=None,
):
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
        out = [(0, 0, 0)] * n
        for i in KEY_LEDS:
            hue = (positions[i] + t * RAINBOW_SPEED * d * s) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, factor)
            out[i] = (int(r * 255), int(g * 255), int(b * 255))
        return out

    if kind == "colorcycle":
        hue = (t * COLORCYCLE_SPEED * d * s) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, factor)
        return [(int(r * 255), int(g * 255), int(b * 255))] * n

    if kind == "wave":
        positions = POS_V if vertical else POS_H
        out = [(0, 0, 0)] * n
        for i in KEY_LEDS:
            phase = positions[i] - t * WAVE_SPEED * d * s
            level = (math.sin(2 * math.pi * phase) + 1) / 2
            out[i] = _scaled(base_rgb, factor * level)
        return out

    if kind == "snake":
        out = [(0, 0, 0)] * n
        path = PATH_V if vertical else PATH_H
        m = len(path)
        head = (t * SNAKE_SPEED * d * s) % m
        for offset in range(SNAKE_LEN):
            pos = int(head - offset * d) % m
            level = max(0.0, 1.0 - offset / SNAKE_LEN)
            out[path[pos]] = _scaled(base_rgb, factor * level)
        return out

    if kind == "dual_snake":
        out = [(0, 0, 0)] * n
        for path, phase in ((PATH_H, 0.0), (PATH_V, 0.5)):
            m = len(path)
            head = (t * SNAKE_SPEED * d * s + phase * m) % m
            for offset in range(SNAKE_LEN):
                pos = int(head - offset * d) % m
                level = max(0.0, 1.0 - offset / SNAKE_LEN)
                candidate = _scaled(base_rgb, factor * level)
                idx = path[pos]
                out[idx] = tuple(max(a, b) for a, b in zip(out[idx], candidate))
        return out

    if kind == "comet":
        # Светящийся фронт с хвостом пролетает через всю клавиатуру
        # (слева направо или сверху вниз) и полностью уходит за край.
        out = [(0, 0, 0)] * n
        glow = _mix(base_rgb, (255, 255, 255), 0.55)
        if vertical:
            span, speed, trail = LED_ROWS - 1, COMET_V_SPEED, COMET_V_TRAIL
        else:
            span, speed, trail = N_COLS - 1, COMET_SPEED, COMET_LEN
        head = (t * speed * s) % (span + trail + 1)
        for i in KEY_LEDS:
            u = LED_XY[i][1] if vertical else LED_XY[i][0]
            if d < 0:
                u = span - u
            behind = head - u
            if 0.0 <= behind < trail:
                level = (1.0 - behind / trail) ** 1.6
                src = glow if behind < 1.0 else base_rgb
                out[i] = _scaled(src, factor * level)
        return out

    if kind == "ripple":
        dist_list = DIST_V if vertical else DIST_H
        out = [(0, 0, 0)] * n
        for i in KEY_LEDS:
            dist = dist_list[i]
            phase = dist * RIPPLE_SPREAD - t * RIPPLE_SPEED * d * s
            level = (math.sin(2 * math.pi * phase) + 1) / 2
            level *= max(0.0, 1.0 - dist * 0.25)
            out[i] = _scaled(base_rgb, factor * level)
        return out

    if kind == "twinkle":
        bucket = int(t * TWINKLE_HZ * s)
        out = [(0, 0, 0)] * n
        for i in KEY_LEDS:
            h = _hash01(i * 97.13 + bucket * 131.7)
            out[i] = _scaled(base_rgb, factor * h**3)
        return out

    if kind == "reactive_ring":
        # Тонкое кольцо бежит от нажатой клавиши во все стороны. Позади
        # кольца света нет: прошло — погасло. Яркость кольца слегка падает
        # по мере удаления.
        out = [(0, 0, 0)] * n
        for origin, t_press in touches or ():
            age = t - t_press
            if age < 0 or age > RING_LIFETIME:
                continue
            o_xy = LED_XY[origin]
            if o_xy is None:
                continue
            far = _far_dist(origin)
            radius = age * RING_SPEED * s
            if radius > far + RING_WIDTH:
                continue  # кольцо уже дошло до самого дальнего края
            fade = 1.0 - 0.45 * min(1.0, radius / max(far, 1.0))
            for i in range(n):
                xy = LED_XY[i]
                if xy is None:
                    continue
                dist = math.hypot(xy[0] - o_xy[0], xy[1] - o_xy[1])
                delta = abs(dist - radius)
                if delta >= RING_WIDTH:
                    continue
                level = (1.0 - delta / RING_WIDTH) ** 0.8 * fade
                candidate = _scaled(base_rgb, factor * level)
                out[i] = tuple(max(a, b) for a, b in zip(out[i], candidate))
        return out

    if _is_reactive_kind(kind):
        # Свет разливается от нажатой клавиши одним цветом, чем дальше —
        # тем тусклее. Расстояния считаются по реальной матрице светодиодов
        # (LED_XY), поэтому волна идёт ровно от нужной клавиши.
        #   reactive          — круг во все стороны по всей клавиатуре
        #   reactive_row_both — по ряду, и вправо, и влево
        row_mode = kind == "reactive_row_both"
        reach = REACTIVE_ROW_REACH if row_mode else REACTIVE_REACH
        out = [(0, 0, 0)] * n
        for origin, t_press in touches or ():
            age = t - t_press
            if age < 0 or age > REACTIVE_LIFETIME:
                continue
            o_xy = LED_XY[origin]
            if o_xy is None:
                continue
            radius = age * REACTIVE_SPEED * s
            fade = (1.0 - age / REACTIVE_LIFETIME) ** 1.2
            for i in range(n):
                xy = LED_XY[i]
                if xy is None:
                    continue
                if row_mode:
                    if xy[1] != o_xy[1]:
                        continue
                    dx = xy[0] - o_xy[0]
                    dist = abs(dx)
                else:
                    dist = math.hypot(xy[0] - o_xy[0], xy[1] - o_xy[1])
                if dist > radius + REACTIVE_EDGE:
                    continue
                level = max(0.0, 1.0 - dist / reach) ** 1.3
                if dist > radius:  # мягкий передний край волны
                    level *= 1.0 - (dist - radius) / REACTIVE_EDGE
                level *= fade
                if level <= 0.0:
                    continue
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

        if exact or keyboard_like:
            # Слушаем ВСЕ клавиатуры, а не только устройства с VID/PID
            # интерфейса подсветки: реальные нажатия могут приходить
            # с другого устройства (другой интерфейс/режим подключения).
            found = exact + keyboard_like
            print(
                "[РЕАКЦИЯ] Слушаю устройства: "
                + ", ".join(f"{d.path} ({d.name})" for d in found)
            )
            return found

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
        print(
            "[РЕАКЦИЯ] Ни одной клавиатуры для чтения не найдено. Видимые устройства:"
        )
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
                "перелогиниться. Системные нажатия не читаются; реагирую "
                "только на клавиши, пока окно программы в фокусе."
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
                # Только само нажатие (value == 1): автоповтор при удержании
                # (value == 2) и движения мыши не должны запускать эффект.
                if event.type == ecodes.EV_KEY and event.value == 1:
                    self.controller.add_touch(_evdev_code_to_idx(event.code))
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
        # Раньше здесь рисовались случайные вспышки — это путало (казалось,
        # что эффект "работает", но не от нажатий). Теперь просто ждём.
        while not self._stop_evt.is_set():
            time.sleep(0.5)

    def stop(self):
        self._stop_evt.set()
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass


class HidrawDevice:
    """Отправка Feature-репорта через /dev/hidrawN.

    В отличие от pyusb здесь НЕ нужно отсоединять ядерный драйвер от
    интерфейса, поэтому остальные функции этого интерфейса (ролик
    громкости, mute и т.п.) продолжают работать как обычно."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)

    def send(self, payload):
        # HIDIOCSFEATURE(len) = _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x06, len)
        req = (3 << 30) | (len(payload) << 16) | (ord("H") << 8) | 0x06
        fcntl.ioctl(self.fd, req, bytes(payload))

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def _find_hidraw_path():
    """Ищет /dev/hidrawN интерфейса USB_INTERFACE нашей клавиатуры."""
    want = f"{VENDOR_ID:08X}:{PRODUCT_ID:08X}"  # HID_ID=0003:0000258A:0000010C
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            dev_dir = os.path.realpath(os.path.join(node, "device"))
            with open(os.path.join(dev_dir, "uevent"), "r") as f:
                uevent = f.read().upper()
            if want not in uevent:
                continue
            with open(os.path.join(dev_dir, "..", "bInterfaceNumber"), "r") as f:
                if int(f.read().strip(), 16) != USB_INTERFACE:
                    continue
            return "/dev/" + os.path.basename(node)
        except Exception:
            continue
    return None


class LeobogController:
    def __init__(self, on_status_change=None):
        self.dev = None
        self.solo_led = None
        self.last_colors = [(0, 0, 0)] * TOTAL_LED_SLOTS
        self._hidraw_failed = False
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
        if _is_reactive_kind(EFFECTS[self.current_effect]["kind"]):
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
        if not self._hidraw_failed:
            path = _find_hidraw_path()
            if path:
                try:
                    dev = HidrawDevice(path)
                    print(f"[USB] Подключено через {path} (драйвер не отсоединяется)")
                    return dev
                except OSError as e:
                    print(
                        f"[USB] Нет доступа к {path}: {e}. Падаю на pyusb — но тогда "
                        "ролик громкости может не работать. Решение — udev-правило (см. README)."
                    )
                    self._hidraw_failed = True
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

        if self.solo_led is not None:
            colors = [(0, 0, 0)] * TOTAL_LED_SLOTS
            colors[self.solo_led] = (255, 255, 255)
        else:
            colors = render_frame(
                self.current_effect,
                self.current_rgb,
                self.brightness,
                t,
                direction=self.direction,
                axis=self.axis,
                speed=self.speed,
                touches=list(self.touches),
            )
        self.last_colors = colors
        for slot, (r, g, b) in enumerate(colors):
            idx = 8 + slot * 3
            if idx + 2 >= PAYLOAD_SIZE:
                break
            payload[idx] = r
            payload[idx + 1] = g
            payload[idx + 2] = b
        return payload

    def _send_locked(self, payload):
        assert len(payload) == PAYLOAD_SIZE, (
            f"[BUG] payload {len(payload)} != {PAYLOAD_SIZE}, отправка отменена"
        )
        if self.dev is None:
            return False
        if isinstance(self.dev, HidrawDevice):
            try:
                self.dev.send(payload)
                return True
            except OSError as e:
                if e.errno in (_errno.ENODEV, _errno.EIO, _errno.EPIPE):
                    print(f"[ERROR] hidraw отвалился (errno={e.errno}): {e}")
                else:
                    print(f"[ERROR] hidraw не принял отчёт ({e}); перехожу на pyusb")
                    self._hidraw_failed = True
                self._release_locked()
                return False
        try:
            self.dev.ctrl_transfer(0x21, 0x09, 0x0306, 0x0001, payload)
            return True
        except usb.core.USBError as e:
            errno = getattr(e, "errno", None)
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
            if isinstance(self.dev, HidrawDevice):
                self.dev.close()
            else:
                try:
                    usb.util.release_interface(self.dev, USB_INTERFACE)
                    usb.util.dispose_resources(self.dev)
                    try:
                        # вернуть интерфейс ядру, чтобы ролик/mute снова работали
                        self.dev.attach_kernel_driver(USB_INTERFACE)
                    except Exception:
                        pass
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

    def set_solo(self, idx):
        """Для калибровки: зажечь только один светодиод (None — выключить режим)."""
        with self._lock:
            self.solo_led = idx
            t = time.monotonic() - self._start_time
            self._send_locked(self._build_payload(t))

    def add_touch(self, idx):
        if idx is None:
            return
        now = time.monotonic() - self._start_time
        with self._lock:
            self.touches.append((idx, now))
            self.touches = [
                (i, tp)
                for (i, tp) in self.touches
                if now - tp < max(REACTIVE_LIFETIME, RING_LIFETIME)
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
        new_r, old_r = _is_reactive_kind(new_kind), _is_reactive_kind(old_kind)
        if new_r and not old_r:
            self._start_reactive_listener()
        elif old_r and not new_r:
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
    "bg": "#0c0c10",
    "panel": "#14141a",
    "panel_alt": "#1d1d26",
    "edge": "#24242e",
    "hover": "#2a2a36",
    "sel_bg": "#1b1f33",
    "accent": "#7c9eff",
    "accent_default": "#7c9eff",
    "accent_dim": "#3a4a85",
    "accent_hi": "#9db4ff",
    "text": "#f0f0f6",
    "subtext": "#8e8e9c",
    "dim": "#4c4c58",
    "ok": "#5ddc97",
    "err": "#ff6b81",
}

# Траектория = ось + направление одним выбором: (подпись, стрелка, ось, знак)
TRAJECTORIES = [
    ("Вправо", "→", "h", 1),
    ("Влево", "←", "h", -1),
    ("Вниз", "↓", "v", 1),
    ("Вверх", "↑", "v", -1),
]
DIR_RIGHT = "Вправо"
DIR_LEFT = "Влево"
DIR_DOWN = "Вниз"
DIR_UP = "Вверх"
AXIS_H = "Гориз."
AXIS_V = "Вертик."

PRESET_COLORS = [
    ("Красный", (255, 0, 0)),
    ("Алый", (255, 45, 45)),
    ("Оранжевый", (255, 120, 0)),
    ("Янтарный", (255, 176, 0)),
    ("Жёлтый", (255, 230, 0)),
    ("Лайм", (150, 255, 0)),
    ("Зелёный", (0, 220, 60)),
    ("Мятный", (0, 255, 150)),
    ("Бирюзовый", (0, 230, 210)),
    ("Голубой", (0, 170, 255)),
    ("Синий", (30, 90, 255)),
    ("Индиго", (90, 60, 255)),
    ("Фиолетовый", (160, 30, 255)),
    ("Пурпурный", (220, 0, 220)),
    ("Розовый", (255, 0, 130)),
    ("Белый", (255, 255, 255)),
]


class ColorWheel(ctk.CTkFrame):
    """Цветовой круг. Всё (диск, обводка, курсор) рисуется через PIL с
    суперсэмплингом и отдаётся в Tk одной готовой картинкой на непрозрачном
    фоне панели — поэтому края гладкие, без зубцов, а курсор чёткий."""

    SS = 4  # суперсэмплинг

    def __init__(self, master, size=156, on_change=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.size = size
        self.radius = size / 2 - 9  # диск; снаружи место под обводку
        self.on_change = on_change
        self._hue = 0.0
        self._sat = 1.0
        self._enabled = True
        self._anim_job = None

        self.canvas = tk.Canvas(
            self,
            width=size,
            height=size,
            highlightthickness=0,
            bg=PALETTE["panel"],
            bd=0,
            cursor="crosshair",
        )
        self.canvas.pack()

        self._base = self._render_base()
        self._photo = ImageTk.PhotoImage(self._base)
        self._img_id = self.canvas.create_image(0, 0, image=self._photo, anchor="nw")

        self.canvas.bind("<Button-1>", self._on_pointer)
        self.canvas.bind("<B1-Motion>", self._on_pointer)
        self._redraw()

    def _render_base(self):
        size, ss = self.size, self.SS
        big = size * ss
        c = big / 2.0
        r = self.radius * ss
        panel = _hex_to_rgb(PALETTE["panel"])

        # диск считаем только внутри его bbox
        disc = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        px = disc.load()
        lo, hi = int(c - r) - 1, int(c + r) + 2
        for y in range(max(0, lo), min(big, hi)):
            dy = y - c
            for x in range(max(0, lo), min(big, hi)):
                dx = x - c
                d = math.hypot(dx, dy)
                if d <= r:
                    hue = ((math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0) / 360.0
                    rr, gg, bb = colorsys.hsv_to_rgb(hue, min(1.0, d / r), 1.0)
                    px[x, y] = (int(rr * 255), int(gg * 255), int(bb * 255), 255)

        img = Image.new("RGBA", (big, big), panel + (255,))
        # тонкая тень под диском
        shadow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).ellipse(
            [c - r - ss, c - r + ss, c + r + ss, c + r + 3 * ss], fill=(0, 0, 0, 90)
        )
        img = Image.alpha_composite(img, shadow)
        img = Image.alpha_composite(img, disc)
        d = ImageDraw.Draw(img)
        # нейтральная тонкая обводка (не зависит от акцента)
        d.ellipse(
            [c - r - ss, c - r - ss, c + r + ss, c + r + ss],
            outline=(255, 255, 255, 38),
            width=max(1, ss),
        )
        return img.resize((size, size), Image.LANCZOS).convert("RGB")

    def _cursor_xy(self):
        cx = cy = self.size / 2.0
        ang = math.radians(self._hue * 360.0)
        dist = self._sat * self.radius
        return cx + dist * math.cos(ang), cy + dist * math.sin(ang)

    def _redraw(self):
        """Курсор: белое кольцо + тёмная кромка + заливка текущим цветом,
        с мягкой тенью. Рисуется в суперсэмплинге на маленьком участке."""
        img = self._base.copy()
        if self._enabled:
            x, y = self._cursor_xy()
            ss = self.SS
            half = 16
            ix, iy = int(x) - half, int(y) - half
            patch = Image.new("RGBA", (2 * half * ss, 2 * half * ss), (0, 0, 0, 0))
            pd = ImageDraw.Draw(patch)
            cx = (x - ix) * ss
            cy = (y - iy) * ss

            def circle(rad, **kw):
                pd.ellipse(
                    [cx - rad * ss, cy - rad * ss, cx + rad * ss, cy + rad * ss], **kw
                )

            circle(10.5, fill=(0, 0, 0, 70))  # тень
            circle(9.5, fill=(10, 10, 14, 255))  # тёмная кромка
            circle(8.3, fill=(255, 255, 255, 255))  # белое кольцо
            circle(6.0, fill=self.get_rgb() + (255,))  # текущий цвет
            patch = patch.resize((2 * half, 2 * half), Image.LANCZOS)
            base_rgba = img.convert("RGBA")
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            layer.paste(patch, (ix, iy))
            img = Image.alpha_composite(base_rgba, layer).convert("RGB")
        self._photo.paste(img)

    def _on_pointer(self, event):
        if not self._enabled:
            return
        cx = cy = self.size / 2.0
        dx, dy = event.x - cx, event.y - cy
        dist = min(math.hypot(dx, dy), self.radius)
        hue = ((math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0) / 360.0
        self.set_hs(hue, dist / self.radius if self.radius else 0.0, notify=True)

    def set_hs(self, hue, sat, notify=False):
        self._hue, self._sat = hue, sat
        self._redraw()
        if notify and self.on_change:
            self.on_change(self.get_rgb())

    def set_rgb(self, rgb):
        r, g, b = (c / 255.0 for c in rgb)
        h, sat, _v = colorsys.rgb_to_hsv(r, g, b)
        self.set_hs(h, sat if sat > 0 else 0.0, notify=False)

    def get_rgb(self):
        r, g, b = colorsys.hsv_to_rgb(self._hue, self._sat, 1.0)
        return (int(r * 255), int(g * 255), int(b * 255))

    def set_enabled(self, enabled):
        if enabled != self._enabled:
            self._enabled = enabled
            self._redraw()

    def animate_to_rgb(self, rgb, duration_ms=280, on_step=None, on_done=None):
        """Плавно 'подъезжает' курсор колеса к позиции статичного цвета."""
        if self._anim_job is not None:
            self.after_cancel(self._anim_job)
            self._anim_job = None
        start_rgb = self.get_rgb()
        start_t = time.monotonic()

        def step():
            frac = min(1.0, (time.monotonic() - start_t) * 1000.0 / duration_ms)
            eased = 1 - (1 - frac) ** 3
            cur = _mix(start_rgb, rgb, eased)
            r, g, b = (c / 255.0 for c in cur)
            h, sat, _v = colorsys.rgb_to_hsv(r, g, b)
            self._hue, self._sat = h, (sat if sat > 0 else 0.0)
            self._redraw()
            if on_step:
                on_step(cur)
            if frac < 1.0:
                self._anim_job = self.after(16, step)
            else:
                self._anim_job = None
                self.set_rgb(rgb)
                if on_done:
                    on_done(self.get_rgb())

        step()


class PresetPalette(ctk.CTkFrame):
    """Ряд готовых цветов. Кружки — сглаженные картинки (PIL), у каждого три
    состояния: обычное, наведение (крупнее) и выбранное (белое кольцо)."""

    SS = 4

    def __init__(
        self, master, colors, on_select=None, swatch=26, gap=8, cols=8, **kwargs
    ):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.colors = colors
        self.on_select = on_select
        self.swatch = swatch
        self.gap = gap
        self.cols = cols
        self.cell = swatch + 8  # запас под кольцо выбора
        rows = math.ceil(len(colors) / cols)
        width = cols * self.cell + (cols - 1) * max(0, gap - 8)
        height = rows * self.cell + (rows - 1) * max(0, gap - 8)
        self._step = self.cell + max(0, gap - 8)

        self.canvas = tk.Canvas(
            self,
            width=width,
            height=height,
            highlightthickness=0,
            bg=PALETTE["panel"],
            bd=0,
            cursor="hand2",
        )
        self.canvas.pack(anchor="w")

        self._items = []
        for idx, (name, rgb) in enumerate(colors):
            col, row = idx % cols, idx // cols
            x0, y0 = col * self._step, row * self._step
            imgs = {
                st: ImageTk.PhotoImage(self._render(rgb, st))
                for st in ("normal", "hover", "selected")
            }
            item_id = self.canvas.create_image(
                x0, y0, image=imgs["normal"], anchor="nw"
            )
            self._items.append(
                {
                    "id": item_id,
                    "rgb": rgb,
                    "name": name,
                    "imgs": imgs,
                    "bbox": (x0, y0, x0 + self.cell, y0 + self.cell),
                    "state": "normal",
                }
            )
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self._selected = None
        self._hover = None

    def _render(self, rgb, state):
        ss, cell = self.SS, self.cell
        big = cell * ss
        c = big / 2.0
        img = Image.new("RGBA", (big, big), _hex_to_rgb(PALETTE["panel"]) + (255,))
        d = ImageDraw.Draw(img)
        r = self.swatch / 2.0 * ss
        if state == "hover":
            r += 1.5 * ss
        if state == "selected":
            ring = r + 3.2 * ss
            d.ellipse(
                [c - ring, c - ring, c + ring, c + ring], fill=(255, 255, 255, 255)
            )
            gap_r = r + 1.6 * ss
            d.ellipse(
                [c - gap_r, c - gap_r, c + gap_r, c + gap_r],
                fill=_hex_to_rgb(PALETTE["panel"]) + (255,),
            )
        d.ellipse([c - r, c - r, c + r, c + r], fill=tuple(rgb) + (255,))
        return img.resize((cell, cell), Image.LANCZOS).convert("RGB")

    def _hit(self, event):
        for item in self._items:
            x0, y0, x1, y1 = item["bbox"]
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                return item
        return None

    def _set_state(self, item, state):
        if item["state"] != state:
            item["state"] = state
            self.canvas.itemconfigure(item["id"], image=item["imgs"][state])

    def _on_click(self, event):
        item = self._hit(event)
        if item is None:
            return
        if self._selected is not None and self._selected is not item:
            self._set_state(self._selected, "normal")
        self._selected = item
        self._set_state(item, "selected")
        if self.on_select:
            self.on_select(item["rgb"], item["name"])

    def _on_motion(self, event):
        item = self._hit(event)
        if item is self._hover:
            return
        if self._hover is not None and self._hover is not self._selected:
            self._set_state(self._hover, "normal")
        if item is not None and item is not self._selected:
            self._set_state(item, "hover")
        self._hover = item

    def _on_leave(self, event):
        if self._hover is not None and self._hover is not self._selected:
            self._set_state(self._hover, "normal")
        self._hover = None

    def clear_selection(self):
        if self._selected is not None:
            self._set_state(self._selected, "normal")
            self._selected = None


class FullWidthDropdown(ctk.CTkFrame):
    """Кнопка-выпадашка, чей список открывается на всю ширину кнопки,
    а не 'куском слева', как это делает стандартный CTkOptionMenu."""

    def __init__(self, master, values, command=None, initial=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.values = list(values)
        self.command = command
        self._current = (
            initial
            if initial in self.values
            else (self.values[0] if self.values else "")
        )
        self._popup = None

        self.button = ctk.CTkButton(
            self,
            text=self._current,
            command=self._toggle,
            fg_color=PALETTE["panel_alt"],
            hover_color=PALETTE["panel_alt"],
            text_color=PALETTE["text"],
            anchor="w",
            corner_radius=10,
            height=36,
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
            self._popup,
            fg_color=PALETTE["panel_alt"],
            corner_radius=10,
            border_width=1,
            border_color=PALETTE["accent"],
        )
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        if height >= 300:
            list_area = ctk.CTkScrollableFrame(outer, fg_color="transparent")
            list_area.pack(fill="both", expand=True, padx=3, pady=3)
        else:
            list_area = ctk.CTkFrame(outer, fg_color="transparent")
            list_area.pack(fill="both", expand=True, padx=3, pady=3)

        for val in self.values:
            is_current = val == self._current
            b = ctk.CTkButton(
                list_area,
                text=val,
                anchor="w",
                fg_color=PALETTE["accent"] if is_current else "transparent",
                hover_color=PALETTE["accent_hi"],
                text_color=("#14141a" if is_current else PALETTE["text"]),
                corner_radius=8,
                height=32,
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


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, c))) for c in rgb)


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _accent_from_rgb(rgb):
    """Акцент интерфейса подстраивается под цвет подсветки."""
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.2 or v < 0.15:
        return PALETTE["accent_default"]
    rr, gg, bb = colorsys.hsv_to_rgb(h, min(1.0, max(s, 0.55)), 1.0)
    return _hex((rr * 255, gg * 255, bb * 255))


class KeyboardPreview(tk.Canvas):
    """Живое превью клавиатуры: каждая клавиша окрашена так же, как её
    светодиод. Работает и без подключённой клавиатуры. Клик по клавише
    запускает на ней реактивный эффект — удобно проверять режимы."""

    CELL = 37
    GAP = 3
    PAD = 14
    KEYCAP = (26, 26, 34)

    def __init__(self, master, on_key=None, **kwargs):
        cols = max(i // LED_ROWS for i in KEYMAP.values()) + 1
        w = self.PAD * 2 + cols * self.CELL + (cols - 1) * self.GAP
        h = self.PAD * 2 + LED_ROWS * self.CELL + (LED_ROWS - 1) * self.GAP
        super().__init__(
            master,
            width=w,
            height=h,
            bg=PALETTE["panel"],
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self.on_key = on_key
        self._keys = []
        self._item_to_idx = {}
        step = self.CELL + self.GAP
        edge = PALETTE["edge"]
        for name, idx in KEYMAP.items():
            col, row = idx // LED_ROWS, idx % LED_ROWS
            c0, c1 = KEY_SPANS.get(name, (col, col))
            x0 = self.PAD + c0 * step
            y0 = self.PAD + row * step
            x1 = self.PAD + c1 * step + self.CELL
            y1 = y0 + self.CELL
            rect = self._rrect(
                x0, y0, x1, y1, 8, fill=_hex(self.KEYCAP), outline=edge, width=1
            )
            label = KEY_LABELS.get(name, name.upper())
            size = 8 if len(label) > 2 else 9
            text = self.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text=label,
                fill="#9a9aa8",
                font=("TkDefaultFont", size, "bold"),
            )
            self._keys.append(
                {"idx": idx, "rect": rect, "text": text, "fill": None, "dark": None}
            )
            self._item_to_idx[rect] = idx
            self._item_to_idx[text] = idx
        self.bind("<Button-1>", self._on_click)
        self.configure(cursor="hand2")

    def _rrect(self, x0, y0, x1, y1, r, **kw):
        pts = [
            x0 + r,
            y0,
            x1 - r,
            y0,
            x1,
            y0,
            x1,
            y0 + r,
            x1,
            y1 - r,
            x1,
            y1,
            x1 - r,
            y1,
            x0 + r,
            y1,
            x0,
            y1,
            x0,
            y1 - r,
            x0,
            y0 + r,
            x0,
            y0,
        ]
        return self.create_polygon(pts, smooth=True, **kw)

    def _on_click(self, event):
        item = self.find_closest(event.x, event.y)
        if item and self.on_key:
            idx = self._item_to_idx.get(item[0])
            if idx is not None:
                self.on_key(idx)

    def update_frame(self, colors):
        base = self.KEYCAP
        for k in self._keys:
            r, g, b = colors[k["idx"]]
            m = max(r, g, b) / 255.0
            fill = _hex(
                tuple(
                    min(255, int(bc * (1.0 - m) + c)) for bc, c in zip(base, (r, g, b))
                )
            )
            if fill != k["fill"]:
                k["fill"] = fill
                self.itemconfigure(k["rect"], fill=fill)
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                dark = lum > 150
                if dark != k["dark"]:
                    k["dark"] = dark
                    self.itemconfigure(k["text"], fill="#0e0e12" if dark else "#9a9aa8")


def _plain_name(name):
    """Название режима без ведущего эмодзи (в плитках они рендерятся криво)."""
    head, _, tail = name.partition(" ")
    return tail if tail and not head.isalnum() else name


class EffectGrid(ctk.CTkFrame):
    """Режимы подсветки плитками — все видны сразу, один клик."""

    def __init__(self, master, values, command=None, initial=None, cols=3, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.command = command
        self._current = initial
        self._accent = PALETTE["accent_default"]
        self._buttons = {}
        for c in range(cols):
            self.grid_columnconfigure(c, weight=1, uniform="eff")
        for i, name in enumerate(values):
            b = ctk.CTkButton(
                self,
                text=_plain_name(name),
                anchor="w",
                height=40,
                corner_radius=11,
                font=ctk.CTkFont(size=12),
                fg_color=PALETTE["panel_alt"],
                hover_color=PALETTE["hover"],
                text_color=PALETTE["text"],
                border_width=1,
                border_color=PALETTE["panel_alt"],
                command=lambda n=name: self._pick(n),
            )
            b.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
            self._buttons[name] = b
        self._refresh()

    def _refresh(self):
        for n, b in self._buttons.items():
            sel = n == self._current
            b.configure(
                border_color=self._accent if sel else PALETTE["panel_alt"],
                fg_color=PALETTE["sel_bg"] if sel else PALETTE["panel_alt"],
            )

    def _pick(self, name):
        self._current = name
        self._refresh()
        if self.command:
            self.command(name)

    def set(self, name):
        self._current = name
        self._refresh()

    def get(self):
        return self._current

    def set_accent(self, hex_color):
        self._accent = hex_color
        self._refresh()


_ARROW_CACHE = {}


def _arrow_icon(axis, sign, color_hex, px=26):
    """Чёткая стрелка (PIL, суперсэмплинг) как иконка для кнопки."""
    key = (axis, sign, color_hex, px)
    img = _ARROW_CACHE.get(key)
    if img is None:
        ss = 6
        big = px * ss
        im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        col = _hex_to_rgb(color_hex) + (255,)
        c = big / 2.0
        L = big * 0.34  # половина длины стрелки
        H = big * 0.20  # размах наконечника
        w = max(2, int(big * 0.085))
        # единичный вектор направления
        ux, uy = {
            ("h", 1): (1, 0),
            ("h", -1): (-1, 0),
            ("v", 1): (0, 1),
            ("v", -1): (0, -1),
        }[(axis, sign)]
        px_, py_ = -uy, ux  # перпендикуляр
        tail = (c - ux * L, c - uy * L)
        tip = (c + ux * L, c + uy * L)
        d.line([tail, tip], fill=col, width=w)
        for side in (1, -1):
            wing = (tip[0] - ux * H + px_ * H * side, tip[1] - uy * H + py_ * H * side)
            d.line([tip, wing], fill=col, width=w)
        r = w / 2.0
        for p in (tail, tip):
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=col)
        for side in (1, -1):
            wing = (tip[0] - ux * H + px_ * H * side, tip[1] - uy * H + py_ * H * side)
            d.ellipse([wing[0] - r, wing[1] - r, wing[0] + r, wing[1] + r], fill=col)
        img = im.resize((px, px), Image.LANCZOS)
        _ARROW_CACHE[key] = img
    return ctk.CTkImage(light_image=img, dark_image=img, size=(px, px))


class PresetChips(ctk.CTkFrame):
    """Ряд быстрых значений под ползунком. Подсвечивается тот, что совпадает
    с текущим значением; ползунок при этом остаётся свободным."""

    def __init__(self, master, values, on_pick, suffix="%", **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.values = list(values)
        self.on_pick = on_pick
        self._accent = PALETTE["accent_default"]
        self._value = None
        self._buttons = {}
        for c in range(len(self.values)):
            self.grid_columnconfigure(c, weight=1, uniform="chip")
        for c, v in enumerate(self.values):
            b = ctk.CTkButton(
                self,
                text=f"{v}{suffix}",
                height=26,
                corner_radius=8,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color=PALETTE["panel_alt"],
                hover_color=PALETTE["hover"],
                text_color=PALETTE["subtext"],
                border_width=1,
                border_color=PALETTE["panel_alt"],
                command=lambda val=v: self.on_pick(val),
            )
            b.grid(row=0, column=c, sticky="ew", padx=2)
            self._buttons[v] = b

    def set_value(self, value):
        value = round(float(value))
        if value == self._value:
            return
        self._value = value
        self._refresh()

    def set_accent(self, hex_color):
        self._accent = hex_color
        self._refresh()

    def _refresh(self):
        for v, b in self._buttons.items():
            sel = v == self._value
            b.configure(
                border_color=self._accent if sel else PALETTE["panel_alt"],
                fg_color=PALETTE["sel_bg"] if sel else PALETTE["panel_alt"],
                text_color=self._accent if sel else PALETTE["subtext"],
            )


class DirectionPad(ctk.CTkFrame):
    """Выбор траектории: четыре плитки со стрелками. Один клик задаёт и ось,
    и направление. Недоступные для текущего режима плитки гаснут."""

    def __init__(self, master, command=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.command = command
        self._current = ("h", 1)
        self._accent = PALETTE["accent_default"]
        self._allowed = {(a, sg) for _l, _ar, a, sg in TRAJECTORIES}
        self._buttons = {}
        for c in range(len(TRAJECTORIES)):
            self.grid_columnconfigure(c, weight=1, uniform="traj")
        for c, (label, arrow, axis, sign) in enumerate(TRAJECTORIES):
            b = ctk.CTkButton(
                self,
                text=label,
                height=64,
                corner_radius=12,
                compound="top",
                image=_arrow_icon(axis, sign, PALETTE["text"]),
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color=PALETTE["panel_alt"],
                hover_color=PALETTE["hover"],
                text_color=PALETTE["text"],
                border_width=1,
                border_color=PALETTE["panel_alt"],
                command=lambda a=axis, sg=sign: self._pick(a, sg),
            )
            b.grid(row=0, column=c, sticky="ew", padx=3)
            self._buttons[(axis, sign)] = b
        self._refresh()

    def _pick(self, axis, sign):
        if (axis, sign) not in self._allowed:
            return
        self._current = (axis, sign)
        self._refresh()
        if self.command:
            self.command(axis, sign)

    def _refresh(self):
        for key, b in self._buttons.items():
            allowed = key in self._allowed
            sel = key == self._current and allowed
            tcol = (
                (self._accent if sel else PALETTE["text"])
                if allowed
                else PALETTE["dim"]
            )
            b.configure(
                image=_arrow_icon(key[0], key[1], tcol),
                border_color=self._accent if sel else PALETTE["panel_alt"],
                fg_color=PALETTE["sel_bg"] if sel else PALETTE["panel_alt"],
                text_color=tcol,
                hover_color=PALETTE["hover"] if allowed else PALETTE["panel_alt"],
                state="normal" if allowed else "disabled",
            )

    def set(self, axis, sign):
        self._current = (axis, 1 if sign >= 0 else -1)
        self._refresh()

    def set_allowed(self, allowed):
        self._allowed = set(allowed)
        self._refresh()

    def set_accent(self, hex_color):
        self._accent = hex_color
        self._refresh()


class App(ctk.CTk):
    def __init__(self, controller: LeobogController):
        super().__init__()
        self.controller = controller
        self.controller.on_status_change = self._on_status_change
        self.quit_callback = None
        self.tray_available = False
        self._rgb_editing = False
        self._accent = PALETTE["accent_default"]
        self._tick_n = 0
        self._chip_groups = []

        self.title("LEOBOG HI75C Studio")
        self.geometry("1080x764")
        self.minsize(1080, 764)
        self.resizable(False, False)
        self.configure(fg_color=PALETTE["bg"])
        self.protocol("WM_DELETE_WINDOW", self.hide_window)

        self._build_ui()
        self._on_status_change(self.controller.is_connected())
        self._tick()

        # Гарантированный источник "нажатий", пока окно в фокусе (работает
        # даже если у программы нет прав на чтение /dev/input).
        self._held = set()
        self._release_pending = {}
        self.bind_all("<KeyPress>", self._on_local_keypress, add="+")
        self.bind_all("<KeyRelease>", self._on_local_keyrelease, add="+")

    # ---------- построение окна ----------
    def _card(self, parent):
        return ctk.CTkFrame(
            parent,
            corner_radius=18,
            fg_color=PALETTE["panel"],
            border_width=1,
            border_color=PALETTE["edge"],
        )

    def _label(self, parent, text):
        return ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=PALETTE["subtext"],
        )

    def _slider_row(
        self, parent, title, from_, to, command, value, suffix="%", presets=None
    ):
        self._label(parent, title).pack(anchor="w", padx=18, pady=(14, 2))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18)
        chips = None

        def on_slide(val):
            command(val)
            if chips is not None:
                chips.set_value(val)

        slider = ctk.CTkSlider(
            row,
            from_=from_,
            to=to,
            command=on_slide,
            height=16,
            progress_color=self._accent,
            button_color=self._accent,
            button_hover_color=PALETTE["text"],
            fg_color=PALETTE["panel_alt"],
        )
        slider.set(value)
        slider.pack(side="left", fill="x", expand=True)
        lbl = ctk.CTkLabel(
            row,
            text=f"{int(value)}{suffix}",
            width=48,
            anchor="e",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=PALETTE["text"],
        )
        lbl.pack(side="left", padx=(10, 0))

        if presets:

            def pick(v):
                slider.set(v)
                on_slide(v)

            chips = PresetChips(parent, presets, pick, suffix=suffix)
            chips.pack(fill="x", padx=15, pady=(8, 0))
            chips.set_value(value)
            self._chip_groups.append(chips)
        return slider, lbl

    def _build_ui(self):
        # --- верхняя полоска: только мелкие кнопки окна и точка-индикатор ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=26, pady=(14, 0))
        for text, cmd, hov in (
            ("✕", self._quit, PALETTE["err"]),
            ("—", self.hide_window, PALETTE["hover"]),
        ):
            ctk.CTkButton(
                header,
                text=text,
                width=30,
                height=26,
                corner_radius=8,
                command=cmd,
                fg_color=PALETTE["panel"],
                hover_color=hov,
                text_color=PALETTE["subtext"],
                border_width=1,
                border_color=PALETTE["edge"],
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(side="right", padx=(6, 0))
        self.status_dot = ctk.CTkLabel(
            header, text="●", font=ctk.CTkFont(size=12), text_color=PALETTE["err"]
        )
        self.status_dot.pack(side="right", padx=(0, 8))

        # --- тело: слева превью + режимы, справа цвет + параметры ---
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=26, pady=(10, 22))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0, minsize=364)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ctk.CTkFrame(body, fg_color="transparent", width=364)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_propagate(False)
        right.pack_propagate(False)

        # режимы
        modes_card = self._card(left)
        modes_card.pack(fill="x")
        self._label(modes_card, "РЕЖИМ").pack(anchor="w", padx=18, pady=(14, 6))
        self.effect_grid = EffectGrid(
            modes_card,
            values=list(EFFECTS.keys()),
            command=self._on_effect_change,
            initial=self.controller.current_effect,
            cols=4,
        )
        self.effect_grid.pack(fill="x", padx=14, pady=(0, 12))

        # превью
        prev_card = self._card(left)
        prev_card.pack(fill="both", expand=True, pady=(14, 0))
        top = ctk.CTkFrame(prev_card, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(14, 0))
        self._label(top, "ПРЕВЬЮ").pack(side="left")
        ctk.CTkLabel(
            top,
            text="клик по клавише — проверить эффект",
            font=ctk.CTkFont(size=10),
            text_color=PALETTE["dim"],
        ).pack(side="right")
        self.preview = KeyboardPreview(prev_card, on_key=self._on_preview_key)
        self.preview.pack(padx=10, pady=(2, 10), expand=True)

        # цвет
        color_card = self._card(right)
        color_card.pack(fill="x")
        self._label(color_card, "ЦВЕТ").pack(anchor="w", padx=18, pady=(14, 4))

        color_row = ctk.CTkFrame(color_card, fg_color="transparent")
        color_row.pack(padx=18, pady=(2, 6), fill="x")
        self.color_wheel = ColorWheel(
            color_row, size=150, on_change=self._on_wheel_change
        )
        self.color_wheel.set_rgb(self.controller.current_rgb)
        self.color_wheel.pack(side="left")

        rgb_col = ctk.CTkFrame(color_row, fg_color="transparent")
        rgb_col.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self.rgb_entries = {}
        for ch in ("R", "G", "B"):
            erow = ctk.CTkFrame(rgb_col, fg_color="transparent")
            erow.pack(fill="x", pady=3)
            ctk.CTkLabel(
                erow,
                text=ch,
                width=14,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=PALETTE["subtext"],
            ).pack(side="left")
            entry = ctk.CTkEntry(
                erow,
                width=66,
                height=28,
                justify="center",
                fg_color=PALETTE["panel_alt"],
                text_color=PALETTE["text"],
                border_width=1,
                border_color=PALETTE["panel_alt"],
                corner_radius=8,
            )
            entry.insert(0, str(self.controller.current_rgb["RGB".index(ch)]))
            entry.bind("<Return>", self._on_rgb_entry_commit)
            entry.bind("<KP_Enter>", self._on_rgb_entry_commit)
            entry.bind("<FocusIn>", lambda e, en=entry: self._on_rgb_focus_in(en))
            entry.bind("<FocusOut>", self._on_rgb_entry_commit)
            entry.pack(side="left", padx=(8, 0))
            self.rgb_entries[ch] = entry
        self.apply_btn = ctk.CTkButton(
            rgb_col,
            text="Применить",
            command=self._on_rgb_entry_commit,
            fg_color=PALETTE["accent_dim"],
            hover_color=PALETTE["hover"],
            text_color=PALETTE["text"],
            corner_radius=8,
            height=28,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.apply_btn.pack(fill="x", pady=(8, 0))

        hex_row = ctk.CTkFrame(color_card, fg_color="transparent")
        hex_row.pack(pady=(2, 6))
        self.hex_swatch = ctk.CTkFrame(
            hex_row,
            width=16,
            height=16,
            corner_radius=5,
            fg_color="#ffffff",
            border_width=1,
            border_color=PALETTE["dim"],
        )
        self.hex_swatch.pack(side="left", padx=(0, 8))
        self.hex_swatch.pack_propagate(False)
        self.color_hex_lbl = ctk.CTkLabel(
            hex_row,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=PALETTE["subtext"],
        )
        self.color_hex_lbl.pack(side="left")

        self.preset_palette = PresetPalette(
            color_card,
            PRESET_COLORS,
            on_select=self._on_preset_click,
            swatch=26,
            gap=7,
            cols=8,
        )
        self.preset_palette.pack(padx=18, pady=(2, 16), anchor="w")

        # параметры
        params_card = self._card(right)
        params_card.pack(fill="x", pady=(14, 0))

        self.brightness_slider, self.brightness_val_lbl = self._slider_row(
            params_card,
            "ЯРКОСТЬ",
            0,
            100,
            self._on_brightness_change,
            self.controller.brightness,
            presets=[0, 25, 50, 75, 100],
        )
        init_speed_pct = max(25, min(400, round(self.controller.speed * 100)))
        self.speed_slider, self.speed_val_lbl = self._slider_row(
            params_card,
            "СКОРОСТЬ",
            25,
            400,
            self._on_speed_change,
            init_speed_pct,
            presets=[25, 50, 100, 200, 400],
        )

        init_axis = "v" if self.controller.axis == "v" else "h"
        init_sign = 1 if self.controller.direction >= 0 else -1

        self.traj_title = self._label(params_card, "ТРАЕКТОРИЯ")
        self.traj_title.pack(anchor="w", padx=18, pady=(16, 6))
        self.dir_pad = DirectionPad(params_card, command=self._on_traj_pick)
        self.dir_pad.set(init_axis, init_sign)
        self.dir_pad.pack(fill="x", padx=15, pady=(0, 18))

        self.tray_hint_lbl = ctk.CTkLabel(
            right,
            text="",
            font=ctk.CTkFont(size=10),
            text_color=PALETTE["dim"],
            wraplength=340,
            justify="left",
        )
        self.tray_hint_lbl.pack(anchor="w", padx=6, pady=(10, 0))

        self._update_ui_state()
        self._apply_accent(self._accent, force=True)

    def _segmented(self, parent, values, command):
        return ctk.CTkSegmentedButton(
            parent,
            values=values,
            command=command,
            fg_color=PALETTE["panel_alt"],
            selected_color=PALETTE["accent_dim"],
            selected_hover_color=PALETTE["accent_dim"],
            unselected_color=PALETTE["panel_alt"],
            unselected_hover_color=PALETTE["hover"],
            text_color=PALETTE["text"],
            corner_radius=10,
            height=32,
        )

    # ---------- акцент, превью, синхронизация ----------
    def _apply_accent(self, hex_color, force=False):
        if hex_color == self._accent and not force:
            return
        self._accent = hex_color
        dim = _hex(
            _mix(_hex_to_rgb(hex_color), _hex_to_rgb(PALETTE["panel_alt"]), 0.55)
        )
        self.effect_grid.set_accent(hex_color)
        for sl in (self.brightness_slider, self.speed_slider):
            sl.configure(progress_color=hex_color, button_color=hex_color)
        self.dir_pad.set_accent(hex_color)
        for g in self._chip_groups:
            g.set_accent(hex_color)
        self.apply_btn.configure(fg_color=dim)

    def _tick(self):
        c = self.controller
        try:
            with c._lock:
                eff = c.current_effect
                rgb = c.current_rgb
                br = c.brightness
                d = c.direction
                ax = c.axis
                sp = c.speed
                touches = list(c.touches)
            t = time.monotonic() - c._start_time
            colors = render_frame(
                eff, rgb, br, t, direction=d, axis=ax, speed=sp, touches=touches
            )
            self.preview.update_frame(colors)
            if self._tick_n % 3 == 0:
                self._update_color_preview()
        except Exception as e:  # превью не должно ронять программу
            if self._tick_n % 300 == 0:
                print(f"[ПРЕВЬЮ] {e}")
        self._tick_n += 1
        self.after(33, self._tick)

    def _update_color_preview(self):
        r, g, b = self.controller.current_rgb
        factor = self.controller.brightness / 100.0
        rr, gg, bb = (int(c * factor) for c in (r, g, b))
        hex_c = f"#{rr:02x}{gg:02x}{bb:02x}"
        if EFFECTS[self.controller.current_effect]["uses_color"]:
            self.color_hex_lbl.configure(
                text=hex_c.upper(), text_color=PALETTE["subtext"]
            )
            self._apply_accent(_accent_from_rgb(self.controller.current_rgb))
        else:
            self._apply_accent(PALETTE["accent_default"])
        self.hex_swatch.configure(fg_color=hex_c)
        self._sync_rgb_entries(self.controller.current_rgb)

    def _sync_rgb_entries(self, rgb):
        if self._rgb_editing:
            return  # пользователь сейчас печатает — не мешаем
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
            entry.configure(border_color=self._accent)

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

        if directional:
            if has_axis:
                allowed = {(ax, sg) for _l, _ar, ax, sg in TRAJECTORIES}
            else:  # ось фиксирована, меняется только направление
                cur_ax = "v" if self.controller.axis == "v" else "h"
                allowed = {(cur_ax, 1), (cur_ax, -1)}
        else:
            allowed = set()
        self.dir_pad.set_allowed(allowed)
        self.dir_pad.set(self.controller.axis, self.controller.direction)
        self.traj_title.configure(
            text_color=PALETTE["subtext"] if directional else PALETTE["dim"]
        )

    # ---------- события ----------
    def _on_status_change(self, connected):
        self.after(0, lambda: self._apply_status(connected))

    def _on_local_keypress(self, event):
        # Гарантированный триггер реактивных эффектов, пока окно в фокусе.
        key = event.keysym
        pend = self._release_pending.pop(key, None)
        if pend is not None:  # релиз+нажатие подряд = автоповтор, не нажатие
            self.after_cancel(pend)
            return
        if key in self._held:  # удержание
            return
        self._held.add(key)
        if not _is_reactive_kind(EFFECTS[self.controller.current_effect]["kind"]):
            return
        self.controller.add_touch(_tk_event_to_idx(event))

    def _on_local_keyrelease(self, event):
        key = event.keysym

        def done():
            self._release_pending.pop(key, None)
            self._held.discard(key)

        self._release_pending[key] = self.after(25, done)

    def _on_preview_key(self, idx):
        # Клик по клавише в превью = нажатие этой клавиши.
        if _is_reactive_kind(EFFECTS[self.controller.current_effect]["kind"]):
            self.controller.add_touch(idx)

    def _apply_status(self, connected):
        self.status_dot.configure(
            text_color=PALETTE["ok"] if connected else PALETTE["err"]
        )

    def _on_effect_change(self, name):
        self.controller.set_effect(name)
        self._update_ui_state()

    def _on_wheel_change(self, rgb):
        self.controller.set_color(rgb)
        self.preset_palette.clear_selection()
        self._update_color_preview()

    def _on_preset_click(self, rgb, name):
        # колесо само "уезжает" (курсор плавно скользит) на выбранный цвет
        def on_step(cur_rgb):
            self.controller.set_color(cur_rgb)
            self._update_color_preview()

        def on_done(final_rgb):
            self.controller.set_color(final_rgb)
            self._update_color_preview()

        self.color_wheel.animate_to_rgb(
            rgb, duration_ms=280, on_step=on_step, on_done=on_done
        )

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

    def _on_traj_pick(self, axis, sign):
        self.controller.set_axis(axis)
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
                "например модуль трея в waybar). Кнопка «—» сворачивает "
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
    dc.rounded_rectangle(
        [8, 8, 56, 56],
        radius=14,
        fill=(25, 25, 29, 255),
        outline=(124, 158, 255, 255),
        width=3,
    )
    dc.ellipse([22, 22, 42, 42], fill=(124, 158, 255, 255))
    return image


def run_calibration():
    """Определяет, какой светодиод под какой клавишей.

    Программа по очереди зажигает ОДИН светодиод (белым). Нужно нажать
    клавишу, под которой он горит. Если под светодиодом клавиши нет или он
    не виден — просто подожди, программа сама пойдёт дальше."""
    import queue

    if not EVDEV_AVAILABLE:
        print("Нужен evdev (pip install evdev).")
        return 1
    pid = _read_lock_pid()
    if _pid_alive(pid):
        print(f"Сначала закрой программу (PID {pid}) — кнопка «Выход» в окне/трее.")
        return 1

    finder = ReactiveListener(None)
    devices = finder._find_devices()
    if not devices:
        print(
            "Не удалось прочитать клавиатуру. Нужна группа input:\n"
            "  sudo usermod -aG input $USER   (потом перелогиниться)"
        )
        return 1

    ctrl = LeobogController()
    print("Жду подключения клавиатуры…")
    for _ in range(50):
        if ctrl.is_connected():
            break
        time.sleep(0.2)
    if not ctrl.is_connected():
        print("Клавиатура не подключена.")
        ctrl.shutdown()
        return 1

    import select

    def poll_key(timeout):
        """Ждёт нажатия до timeout секунд, возвращает код клавиши или None.
        Читает прямо здесь (без фоновых потоков), ошибки не глотает."""
        deadline = time.monotonic() + timeout
        while True:
            left = max(0.0, deadline - time.monotonic())
            try:
                ready, _, _ = select.select(devices, [], [], left)
            except (OSError, ValueError) as e:
                print(f"\n[КАЛИБРОВКА] select не удался: {e}")
                return None
            found = None
            for d in ready:
                try:
                    for ev in d.read():
                        if ev.type == ecodes.EV_KEY and ev.value == 1 and found is None:
                            found = ev.code
                except BlockingIOError:
                    pass
                except OSError as e:
                    print(f"\n[КАЛИБРОВКА] Ошибка чтения {d.path}: {e}")
                    devices.remove(d)
            if found is not None:
                return found
            if left <= 0.0 or not devices:
                return None

    wait_s = 1.6
    mapping = {}
    print(
        f"\nКалибровка: горит один светодиод — нажми клавишу над ним (ждём {wait_s} с)."
    )
    print("Пустые/невидимые светодиоды пропускаются сами. Ctrl+C — отмена.\n")
    try:
        for idx in range(TOTAL_LED_SLOTS):
            while poll_key(0.0) is not None:  # сбросить накопившиеся нажатия
                pass
            ctrl.set_solo(idx)
            print(f"\rСветодиод {idx + 1}/{TOTAL_LED_SLOTS}   ", end="", flush=True)
            code = poll_key(wait_s)
            if code is None:
                continue
            logical = EVDEV_KEY_TO_LOGICAL.get(code)
            if logical is None:
                print(f"\n  клавиша с кодом {code} не поддерживается — пропуск")
                continue
            mapping[logical] = idx
            print(f"\n  светодиод {idx} = {logical}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nОтменено, карта не сохранена.")
        ctrl.set_solo(None)
        ctrl.shutdown()
        return 1

    ctrl.set_solo(None)
    ctrl.shutdown()
    if not mapping:
        print("\nНи одна клавиша не нажата — карта не сохранена.")
        return 1
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(KEYMAP_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=1)
    missing = [k for k in DEFAULT_KEYMAP if k not in mapping]
    print(f"\nГотово: сопоставлено клавиш — {len(mapping)}, файл {KEYMAP_PATH}")
    if missing:
        print("Не сопоставлены (на них эффект не сработает):", ", ".join(missing))
    return 0


def run_keytest():
    """Показывает, с каких устройств реально приходят нажатия."""
    import select

    if not EVDEV_AVAILABLE:
        print("Нужен evdev.")
        return 1
    devices = ReactiveListener(None)._find_devices()
    if not devices:
        print(
            "Нет доступных устройств. Нужна группа input: "
            "sudo usermod -aG input $USER и перелогиниться."
        )
        return 1
    print("\nЖми клавиши (Ctrl+C — выход). Показываю устройство и код.\n")
    try:
        while True:
            ready, _, _ = select.select(devices, [], [])
            for d in ready:
                try:
                    for ev in d.read():
                        if ev.type == ecodes.EV_KEY and ev.value == 1:
                            print(
                                f"{d.path} ({d.name}): код {ev.code} = "
                                f"{EVDEV_KEY_TO_LOGICAL.get(ev.code, '?')}"
                            )
                except BlockingIOError:
                    pass
    except KeyboardInterrupt:
        return 0


def _parse_args():
    parser = argparse.ArgumentParser(description="LEOBOG HI75C Studio")
    parser.add_argument(
        "--keytest",
        action="store_true",
        help="Показать, с каких устройств приходят нажатия (диагностика).",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Определить, какой светодиод под какой клавишей (для эффекта "
        "«Реакция на нажатие»).",
    )
    parser.add_argument(
        "--tray",
        "--autostart",
        "--minimized",
        dest="start_in_tray",
        action="store_true",
        help="Запуститься сразу свёрнутым в трей, не открывая окно "
        "настроек. Для автозапуска вместе с Hyprland/DE: "
        "exec-once = /путь/до/папки/run.sh --tray",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    if args.keytest:
        sys.exit(run_keytest())
    if args.calibrate:
        sys.exit(run_calibration())

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
