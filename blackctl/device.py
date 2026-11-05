"""Headset access through the blackctl driver's sysfs attributes, plus saved settings."""

import glob
import json
import os

DRIVER_DIR = '/sys/bus/hid/drivers/blackctl'
CONFIG_FILE = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'),
                           'blackctl.json')

# USB product id -> (name, family). Only the Xbox dongle is tested on hardware.
MODELS = {
    '0A55': ('BlackShark V3 Pro for Xbox', 'xbox'),
    '0577': ('BlackShark V3 Pro', 'v3pro'),
    '0576': ('BlackShark V3 Pro (USB-C)', 'v3pro'),
    '057A': ('BlackShark V3', 'v3'),
    '0579': ('BlackShark V3 (USB-C)', 'v3'),
}


def read_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_config(data):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


class Device:
    """A connected headset. Values read as strings; '-1' means not reported yet."""

    def __init__(self, path):
        self.path = path
        self.pid = os.path.basename(path).split(':')[2].split('.')[0].upper()
        self.name, self.family = MODELS[self.pid]

    @classmethod
    def detect(cls):
        for path in sorted(glob.glob(f'{DRIVER_DIR}/0003:1532:*')):
            if os.path.basename(path).split(':')[2].split('.')[0].upper() in MODELS:
                return cls(path)
        return None

    def has(self, attr):
        return os.path.exists(os.path.join(self.path, attr))

    def read(self, attr):
        try:
            with open(os.path.join(self.path, attr)) as f:
                value = f.read().strip()
        except OSError:
            return None
        return None if value in ('', '-1') or value.startswith('-1 ') else value

    def read_int(self, attr):
        value = self.read(attr)
        try:
            return int(value.split()[0]) if value else None
        except ValueError:
            return None

    def write(self, attr, value):
        try:
            with open(os.path.join(self.path, attr), 'w') as f:
                f.write(str(value))
            return None
        except OSError as e:
            return e.strerror or str(e)


def battery():
    """(name, percent, charging), or None when no headset has reported."""
    dev = Device.detect()
    level = dev.read_int('battery') if dev else None
    if level is None:
        return None
    return dev.name, level, dev.read_int('charging') == 1


def battery_text(as_json=False):
    """Status-bar line: plain text (polybar and others) or waybar JSON. Empty when off."""
    state = battery()
    if not as_json:
        return '' if state is None else f'{state[1]}%' + (' +' if state[2] else '')
    if state is None:
        return json.dumps({'text': '', 'tooltip': 'Headset not connected', 'class': 'disconnected'})
    name, level, charging = state
    cls = 'charging' if charging else ('low' if level <= 15 else 'normal')
    tooltip = f'{name}: {level}%' + (' (charging)' if charging else '')
    return json.dumps({'text': f'{level}%', 'tooltip': tooltip, 'class': cls, 'percentage': level})
