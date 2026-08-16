#!/usr/bin/env bash
# Ставит venv и зависимости при первом запуске, дальше просто запускает.
# Автозапуск Hyprland: exec-once = /путь/до/папки/run.sh --tray  в hyprland.conf
#   (флаг --tray запускает сразу свёрнутым в трей, без окна на каждый вход;
#    последний выбранный режим/цвет включится сам)
# Автозапуск других DE: добавь "run.sh --tray" в Startup Applications / autostart.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -d venv ]; then
    python3 -m venv --system-site-packages venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install pyusb customtkinter pystray pillow evdev
fi

exec ./venv/bin/python app.py "$@"
