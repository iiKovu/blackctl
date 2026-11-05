"""GTK 4 control panel for the BlackShark V3 family."""

import queue
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, GLib, Gtk

from . import fx
from .device import MODELS, Device, read_config, write_config

PROJECT_URL = 'https://github.com/iiKovu/blackctl'

EQ_FREQS = ['31', '63', '125', '250', '500', '1k', '2k', '4k', '8k', '16k']
EQ_NAMES = ['Default', 'Game', 'Movie', 'Music', 'Esports']
# Band gains of the factory presets, as the driver sends them (V3 Pro and Xbox).
PRO_PRESETS = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    [2, 2, 5, 5, 1, -1, 2, 3, 3, 3],
    [3, 3, 3, -1, -4, -4, 2, 3, 3, 3],
    [2, 2, 0, 0, 1, -1, -1, 3, 3, 3],
    [1, 1, -1, 0, 2, 0, 4, 4, 4, -3],
]
# The V3 stores no presets; these are the starting curves for its five slots.
V3_PRESETS = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    [2, 2, -3, -3, 1, -1, 2, 3, 3, 3],
    [3, 3, 4, 0, -4, -4, 2, 3, 3, 3],
    [6, 3, 4, 3, 0, 0, 0, 1, 3, 4],
    [1, 1, -1, 0, 2, 0, 4, 4, 4, -3],
]
XBOX_CUSTOM_SLOT = 5
FN_MODES = [(0, 'Game/Chat'), (1, 'Mic Sidetone'), (2, 'Footsteps'), (3, 'Bluetooth Volume')]
SLEEP_TIMES = [(0, 'Never'), (15, '15 min'), (30, '30 min'), (45, '45 min'), (60, '60 min')]
# Settings pushed back to the headset by Resync, in the driver's attribute names.
SETTINGS = ('eq', 'thx', 'anc', 'sidetone', 'fn_button', 'audio_prompts', 'power_save',
            'ull', 'game_chat')

CSS = b"""
notebook > header > tabs > tab { padding: 8px 18px; }
notebook > header > tabs > tab:checked { color: #00ff41; }
.card { background: alpha(currentColor, 0.05); border-radius: 6px; padding: 14px; }
.title { color: #00ff41; font-weight: bold; font-size: 11px; letter-spacing: 1px; }
.hint { color: #888; font-size: 11px; }
.value { color: #00ff41; font-size: 11px; font-weight: bold; }
scale trough highlight { background: #00ff41; }
button.choice:checked { background: #003a12; color: #00ff41; box-shadow: inset 0 0 0 1px #00ff41; }
.error { color: #ff5555; }
"""


def card(title, hint=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.add_css_class('card')
    label = Gtk.Label(label=title, xalign=0)
    label.add_css_class('title')
    box.append(label)
    box.hint = Gtk.Label(label=hint or '', xalign=0, wrap=True, visible=bool(hint))
    box.hint.add_css_class('hint')
    box.append(box.hint)
    return box


def page(*cards):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for side in ('top', 'bottom', 'start', 'end'):
        getattr(box, f'set_margin_{side}')(16)
    for c in cards:
        box.append(c)
    scroll = Gtk.ScrolledWindow()
    scroll.set_child(box)
    return scroll


class Choice:
    """A row of mutually exclusive toggle buttons; on_pick(value) runs on user clicks only."""

    def __init__(self, options, on_pick):
        self.box = Gtk.Box(spacing=6)
        self.buttons = {}
        self._quiet = False
        first = None
        for value, label in options:
            b = Gtk.ToggleButton(label=label)
            b.add_css_class('choice')
            if first:
                b.set_group(first)
            first = first or b
            b.connect('toggled', self._toggled, value, on_pick)
            self.box.append(b)
            self.buttons[value] = b

    def _toggled(self, button, value, on_pick):
        if button.get_active() and not self._quiet:
            on_pick(value)

    def set(self, value):
        if value in self.buttons and not self.buttons[value].get_active():
            self._quiet = True
            self.buttons[value].set_active(True)
            self._quiet = False


class Slider:
    """Horizontal scale whose on_change(value) runs on user moves only, debounced."""

    def __init__(self, lo, hi, on_change, delay=250, fmt=None):
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
        self.scale.set_hexpand(True)
        self.scale.set_draw_value(True)
        self.scale.set_digits(0)
        if fmt:
            self.scale.set_format_value_func(lambda _s, v: fmt(int(v)))
        self._quiet = False
        self._timer = None
        self._delay = delay
        self.scale.connect('value-changed', self._changed, on_change)

    def _changed(self, scale, on_change):
        if self._quiet:
            return
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(self._delay, self._fire, on_change)

    def _fire(self, on_change):
        self._timer = None
        on_change(int(round(self.scale.get_value())))
        return False

    def set(self, value):
        if value is not None and int(self.scale.get_value()) != value:
            self._quiet = True
            self.scale.set_value(value)
            self._quiet = False


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='blackctl')
        self.set_default_size(860, 620)
        self.set_icon_name('blackctl')

        cfg = read_config()
        self.pid = cfg.get('last_device', '0A55')
        self.family = MODELS[self.pid][1]
        self.saved = self._load_saved(cfg)
        self.eq_custom = cfg.get('eq_custom', {})
        self.fx_state = fx.default_state() | cfg.get('fx', {})
        self.device = None
        self.seen = {}

        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

        notebook = Gtk.Notebook(vexpand=True)
        notebook.append_page(self._sound_tab(), Gtk.Label(label='SOUND'))
        notebook.append_page(self._effects_tab(), Gtk.Label(label='EFFECTS'))
        notebook.append_page(self._mic_tab(), Gtk.Label(label='MIC'))
        notebook.append_page(self._power_tab(), Gtk.Label(label='POWER'))
        self.device_pages = [notebook.get_nth_page(i) for i in (0, 2, 3)]

        about = Gtk.Button(label='About', margin_start=8)
        about.add_css_class('flat')
        about.connect('clicked', self._about)
        notebook.set_action_widget(about, Gtk.PackType.START)
        self.battery_label = Gtk.Label(margin_end=12)
        self.battery_label.add_css_class('value')
        notebook.set_action_widget(self.battery_label, Gtk.PackType.END)

        self.status = Gtk.Label(xalign=0, hexpand=True)
        self.status.add_css_class('hint')
        resync = Gtk.Button(label='Resync to headset')
        resync.set_tooltip_text('Send every saved setting to the headset again')
        resync.connect('clicked', lambda _b: self._resync())
        bar = Gtk.Box(spacing=8, margin_start=16, margin_end=16, margin_bottom=10)
        bar.append(self.status)
        bar.append(resync)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(notebook)
        root.append(bar)
        self.set_child(root)

        self._adapt(None)
        self._poll()
        GLib.timeout_add(1000, self._poll)

    # ── settings and background writes ─────────────────────────────────────

    def _load_saved(self, cfg):
        saved = cfg.get('state', {}).get(self.pid, {})
        if 'audio_fn_button' in saved:  # name used before 0.2
            saved.setdefault('fn_button', saved.pop('audio_fn_button'))
        return saved

    def _save(self):
        cfg = read_config()
        cfg.setdefault('state', {})[self.pid] = self.saved
        cfg['last_device'] = self.pid
        cfg['eq_custom'] = self.eq_custom
        cfg['fx'] = self.fx_state
        write_config(cfg)

    def _worker(self):
        while True:
            func, args = self.jobs.get()
            error = func(*args)
            if error:
                GLib.idle_add(self._set_status, error, True)

    def _set_status(self, text, error=False):
        self.status.set_text(text)
        (self.status.add_css_class if error else self.status.remove_css_class)('error')
        return False

    def _set(self, attr, value):
        """Remember a setting and send it to the headset in the background."""
        self.saved[attr] = str(value)
        self._save()
        device = self.device
        if device:
            def job():
                error = device.write(attr, value)
                return error and f'{attr}: {error}'
            self.jobs.put((job, ()))

    def _resync(self):
        device = self.device
        if not device:
            self._set_status('Headset not found', True)
            return
        for attr in SETTINGS:
            if attr in self.saved and device.has(attr):
                self.jobs.put((device.write, (attr, self.saved[attr])))
        self._set_status('Settings sent to the headset')

    # ── Sound ──────────────────────────────────────────────────────────────

    def _sound_tab(self):
        self.eq_card = card('EQUALIZER')
        self.eq_choice = Choice([(i, n) for i, n in enumerate(EQ_NAMES)]
                                + [(XBOX_CUSTOM_SLOT, 'Custom')], self._pick_preset)
        self.eq_card.append(self.eq_choice.box)
        bands = Gtk.Box(spacing=4, homogeneous=True)
        self.eq_sliders = []
        for freq in EQ_FREQS:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            s = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -6, 6, 1)
            s.set_inverted(True)
            s.set_size_request(-1, 150)
            s.set_value_pos(Gtk.PositionType.TOP)
            s.add_mark(0, Gtk.PositionType.RIGHT, None)
            s.connect('value-changed', self._band_moved)
            col.append(s)
            label = Gtk.Label(label=freq)
            label.add_css_class('hint')
            col.append(label)
            bands.append(col)
            self.eq_sliders.append(s)
        self.eq_card.append(bands)
        self.eq_reset = Gtk.Button(label='Flatten', halign=Gtk.Align.END)
        self.eq_reset.connect('clicked', lambda _b: self._write_bands([0] * 10))
        self.eq_card.append(self.eq_reset)
        self._eq_quiet = False
        self._eq_timer = None

        self.thx_card = card('THX SPATIAL AUDIO', "The headset's own THX mode.")
        self.thx_choice = Choice([(0, 'Stereo'), (1, 'THX')], lambda v: self._set('thx', v))
        self.thx_card.append(self.thx_choice.box)

        self.anc_card = card('NOISE CANCELLATION')
        self.anc_choice = Choice([(0, 'Off'), (1, 'ANC'), (2, 'Ambient')], self._pick_anc)
        self.anc_card.append(self.anc_choice.box)
        self.anc_level = Choice([(n, f'Level {n}') for n in range(1, 5)], self._pick_anc_level)
        self.anc_card.append(self.anc_level.box)

        self.ull_card = card('ULTRA-LOW LATENCY', 'Lower wireless latency at some cost to range and battery.')
        self.ull_choice = Choice([(0, 'Off'), (1, 'On')], lambda v: self._set('ull', v))
        self.ull_card.append(self.ull_choice.box)

        self.game_chat_card = card('GAME / CHAT BALANCE', 'Centre plays both at full volume.')
        self.game_chat = Slider(0, 20, lambda v: self._set('game_chat', v), fmt=lambda v: str(v - 10))
        self.game_chat_card.append(self.game_chat.scale)
        return page(self.eq_card, self.thx_card, self.anc_card, self.ull_card, self.game_chat_card)

    def _presets(self):
        return V3_PRESETS if self.family == 'v3' else PRO_PRESETS

    def _bands_for(self, slot):
        if self.family == 'v3' or slot == XBOX_CUSTOM_SLOT:
            default = self._presets()[slot] if slot < 5 else [0] * 10
            return self.eq_custom.get(str(slot), default)
        return self._presets()[slot]

    def _show_bands(self, values):
        self._eq_quiet = True
        for s, v in zip(self.eq_sliders, values):
            s.set_value(v)
        self._eq_quiet = False

    def _pick_preset(self, slot):
        if self.family == 'v3' or slot == XBOX_CUSTOM_SLOT:
            self._write_bands(self._bands_for(slot), slot)
        else:
            self._show_bands(self._presets()[slot])
            self._set('eq', slot)

    def _editable_slot(self):
        """Slot the sliders write to: the selected one on the V3, Custom on the Xbox."""
        if self.family == 'v3':
            return next((s for s, b in self.eq_choice.buttons.items() if b.get_active()), 1)
        return XBOX_CUSTOM_SLOT

    def _band_moved(self, _scale):
        if self._eq_quiet:
            return
        self.eq_choice.set(self._editable_slot())
        if self._eq_timer:
            GLib.source_remove(self._eq_timer)
        self._eq_timer = GLib.timeout_add(300, self._bands_settled)

    def _bands_settled(self):
        self._eq_timer = None
        self._write_bands([int(round(s.get_value())) for s in self.eq_sliders])
        return False

    def _write_bands(self, values, slot=None):
        slot = self._editable_slot() if slot is None else slot
        self.eq_custom[str(slot)] = list(values)
        self._show_bands(values)
        self.eq_choice.set(slot)
        self._set('eq', f'{slot} ' + ' '.join(map(str, values)))

    def _pick_anc(self, mode):
        level = int((self.saved.get('anc') or '0 4').split()[-1])
        self._set('anc', f'{mode} {level}')

    def _pick_anc_level(self, level):
        self.anc_choice.set(1)
        self._set('anc', f'1 {level}')

    # ── Effects ────────────────────────────────────────────────────────────

    def _effects_tab(self):
        hint = None if fx.available() else 'Effects are not installed: run install.sh again.'
        surround = card('VIRTUAL SURROUND',
                        'Places stereo sound around you, like speakers in a room. '
                        'Done in software, separate from THX.')
        self.surround_choice = Choice([(False, 'Off'), (True, 'On')], self._pick_surround)
        surround.append(self.surround_choice.box)

        audio = card('AUDIO ENHANCEMENT', 'Applied to everything played on the headset.')
        self.audio_mode = Choice([(None, 'Off')] + list(fx.AUDIO_MODES.items()),
                                 lambda m: self._pick_fx_mode('audio', m))
        self.audio_level = Slider(0, 100, lambda v: self._fx_level('audio', v))
        audio.append(self.audio_mode.box)
        audio.append(self.audio_level.scale)

        mic = card('MIC ENHANCEMENT', 'Applied to the headset microphone for every app.')
        self.mic_mode = Choice([(None, 'Off')] + list(fx.MIC_MODES.items()),
                               lambda m: self._pick_fx_mode('mic', m))
        self.mic_level = Slider(0, 100, lambda v: self._fx_level('mic', v))
        mic.append(self.mic_mode.box)
        mic.append(self.mic_level.scale)

        self.fx_status = Gtk.Label(label=hint or '', xalign=0, wrap=True)
        self.fx_status.add_css_class('error')
        return page(surround, audio, mic, self.fx_status)

    def _show_fx(self):
        st = self.fx_state
        self.surround_choice.set(st['surround'])
        for side, choice, slider in (('audio', self.audio_mode, self.audio_level),
                                     ('mic', self.mic_mode, self.mic_level)):
            mode = st[side].get('mode')
            choice.set(mode)
            gate = mode == 'gate'
            slider.scale.set_range(1 if gate else 0, 3 if gate else 100)
            slider.scale.set_format_value_func(
                (lambda _s, v: fx.GATE_LEVELS[int(v)]) if gate else (lambda _s, v: str(int(v))))
            slider.set(fx.level_of(st[side]))
            slider.scale.set_sensitive(mode is not None)

    def _apply_fx(self, side=None):
        self._save()
        state = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.fx_state.items()}

        def job():
            error = fx.apply(state, side)
            GLib.idle_add(self.fx_status.set_text, error or '')
        self.jobs.put((job, ()))

    def _pick_surround(self, on):
        self.fx_state['surround'] = on
        self._apply_fx()

    def _pick_fx_mode(self, side, mode):
        self.fx_state[side]['mode'] = mode
        self._show_fx()
        self._apply_fx(side)

    def _fx_level(self, side, value):
        mode = self.fx_state[side].get('mode')
        if mode:
            self.fx_state[side].setdefault('levels', {})[mode] = value
            self._apply_fx(side)

    # ── Mic and power ──────────────────────────────────────────────────────

    def _mic_tab(self):
        sidetone = card('SIDETONE', 'How much of your own voice you hear (0–15).')
        self.sidetone = Slider(0, 15, lambda v: self._set('sidetone', v), delay=150)
        sidetone.append(self.sidetone.scale)

        fn = card('FUNCTION BUTTON', 'What the headset dial controls.')
        self.fn_choice = Choice(FN_MODES, lambda v: self._set('fn_button', v))
        fn.append(self.fn_choice.box)

        prompts = card('AUDIO PROMPTS', 'Voice prompts for mic mute and other events.')
        self.prompts_choice = Choice([(0, 'Off'), (1, 'On')], lambda v: self._set('audio_prompts', v))
        prompts.append(self.prompts_choice.box)
        return page(sidetone, fn, prompts)

    def _power_tab(self):
        self.sleep_card = card('SLEEP TIMER', 'Turn the headset off after this long without audio.')
        self.sleep_choice = Choice(SLEEP_TIMES, lambda v: self._set('power_save', v))
        self.sleep_card.append(self.sleep_choice.box)
        return page(self.sleep_card)

    # ── state ──────────────────────────────────────────────────────────────

    def _adapt(self, device):
        """Show the controls the connected model has, and load its saved settings."""
        if device and device.pid != self.pid:
            self.pid, self.family = device.pid, device.family
            self.saved = self._load_saved(read_config())
            self._save()
        has = (lambda attr: device.has(attr)) if device else (lambda attr: attr in self.saved)
        xbox, v3 = self.family == 'xbox', self.family == 'v3'

        self.eq_choice.buttons[XBOX_CUSTOM_SLOT].set_visible(xbox)
        for s in self.eq_sliders:
            s.set_sensitive(self.family != 'v3pro')
        self.eq_reset.set_visible(self.family != 'v3pro')
        self.eq_card.hint.set_text({
            'xbox': 'Presets are stored on the headset. Moving a slider edits Custom; '
                    'the factory presets stay as they are.',
            'v3pro': 'Presets are stored on the headset; their bands cannot be edited from Linux yet.',
            'v3': 'Pick a preset and move the sliders to change it.',
        }[self.family])
        self.eq_card.hint.set_visible(True)
        self.anc_card.set_visible(has('anc'))
        self.anc_choice.buttons[2].set_visible(self.family == 'v3pro')
        self.ull_card.set_visible(has('ull'))
        self.game_chat_card.set_visible(has('game_chat'))
        self.sleep_choice.buttons[0].set_visible(not v3)
        self.sleep_choice.box.set_visible(has('power_save'))
        self.sleep_card.hint.set_text('Turn the headset off after this long without audio.'
                                      if has('power_save') else 'Only available on the wireless dongle.')

        self.seen = {}
        for attr in SETTINGS:
            self._show(attr, self.saved.get(attr))
        self._show_fx()

    def _show(self, attr, raw):
        """Mirror one setting (saved or reported by the headset) in its widget."""
        if raw is None:
            return
        parts = str(raw).split()
        try:
            first = int(parts[0])
        except (IndexError, ValueError):
            return
        if attr == 'eq':
            if first in self.eq_choice.buttons:
                self.eq_choice.set(first)
                self._show_bands(self._bands_for(first))
        elif attr == 'anc':
            self.anc_choice.set(first)
            if len(parts) > 1:
                self.anc_level.set(int(parts[1]))
        else:
            {'thx': self.thx_choice, 'fn_button': self.fn_choice, 'audio_prompts': self.prompts_choice,
             'power_save': self.sleep_choice, 'sidetone': self.sidetone, 'ull': self.ull_choice,
             'game_chat': self.game_chat}[attr].set(first)

    def _poll(self):
        """Every second: follow hot-plug, on-board button changes and battery (all cache reads)."""
        device = Device.detect()
        if (device and device.path) != (self.device and self.device.path):
            self.device = device
            if device:
                self._adapt(device)
            self._set_status(f'{device.name} connected' if device
                             else 'Headset not found: plug it in or connect the dongle', not device)
        for widget in self.device_pages:
            widget.set_sensitive(device is not None)
        if not device:
            self.battery_label.set_text('')
            return True

        # Only mirror values the headset changed since the last poll, so a pick that
        # is still being written is not reverted to the old value.
        for attr in SETTINGS:
            value = device.read(attr)
            if value is None or value == self.seen.get(attr):
                continue
            self.seen[attr] = value
            # Keep saved band strings for slots whose bands we write (Xbox Custom, V3).
            if attr != 'eq' or self.family == 'v3pro' or (self.family == 'xbox' and int(value) < 5):
                self.saved[attr] = value
            self._show(attr, value)
        level = device.read_int('battery')
        if level is None:
            self.battery_label.set_text('battery …' if device.has('battery') else '')
        else:
            charging = device.read_int('charging') == 1
            self.battery_label.set_text(f'battery {level}%' + (' · charging' if charging else ''))
        return True

    def _about(self, _button):
        about = Gtk.AboutDialog(transient_for=self, modal=True, program_name='blackctl',
                                logo_icon_name='blackctl', license_type=Gtk.License.GPL_2_0,
                                comments='Razer BlackShark V3 controls for Linux.', website=PROJECT_URL)
        about.present()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.github.iikovu.blackctl')

    def do_activate(self):
        window = self.get_active_window()
        if not window:
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            window = Window(self)
        window.present()


def main():
    return App().run([])
