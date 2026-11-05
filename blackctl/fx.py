"""Software audio effects for the headset: virtual surround, audio and mic enhancement.

The effects run as one standalone PipeWire filter-chain process (blackctl-fx.service) whose
nodes are WirePlumber smart filters targeting the headset's own sink and mic. Everything that
plays to, or records from, the headset passes through them; the default devices never change,
and nothing is written to ~/.config/pipewire.

Effect values and the razer_dyn plugin come from Lynapse 2.0 (github.com/Stealthee/Lynapse2).
"""

import json
import os
import subprocess

PLUGIN = '/usr/lib/blackctl/razer_dyn.so'
HRIR = '/usr/share/blackctl/kemar.wav'  # MIT KEMAR HRTF data, Gardner & Martin 1994
SERVICE = 'blackctl-fx.service'
STATE_DIR = os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'),
                         'blackctl')
CONF = os.path.join(STATE_DIR, 'fx.conf')
HEADSET_NODE = 'BlackShark_V3'  # matches every model's ALSA node names

GAME_NODES = ('blackctl.game', 'blackctl.game.output')
MIC_NODES = ('blackctl.mic.input', 'blackctl.mic')

AUDIO_MODES = {'bass': 'Bass Boost', 'clarity': 'Voice Clarity', 'normalization': 'Sound Normalization'}
MIC_MODES = {'noise': 'Noise Cancellation', 'gate': 'Voice Gate', 'clarity': 'Vocal Clarity',
             'normalization': 'Volume Normalization'}
GATE_LEVELS = {1: 'Low', 2: 'Medium', 3: 'High'}

DYN, LOWSHELF, HIGHSHELF, PEAKING = 'dyn', 'bq_lowshelf', 'bq_highshelf', 'bq_peaking'
AUDIO_CHAIN = (('bass', LOWSHELF), ('vc_shelf', LOWSHELF), ('vc_dip', PEAKING),
               ('vc_pres', PEAKING), ('norm', DYN), ('lim', DYN))
MIC_CHAIN = (('nc', DYN), ('gate', DYN), ('vc_low', LOWSHELF), ('vc_mid', PEAKING),
             ('vc_air', HIGHSHELF), ('norm', DYN), ('lim', DYN))

DYN_PORTS = {
    'gain': ('Input gain (dB)', 0.0), 'exp_t': ('Expander threshold (dB)', -120.0),
    'exp_r': ('Expander ratio', 1.0), 'floor': ('Expander floor (dB)', -200.0),
    'mute': ('Mute threshold (dB)', -200.0), 'comp_t': ('Compressor threshold (dB)', 0.0),
    'comp_r': ('Compressor ratio', 1.0), 'knee': ('Knee (dB)', 0.0),
    'attack': ('Attack (ms)', 10.0), 'release': ('Release (ms)', 100.0),
    'peak': ('Peak detect', 0.0), 'window': ('Detector window (ms)', 10.0),
}
BQ_FREQ = {'bass': 105.0, 'vc_shelf': 169.0, 'vc_dip': 151.0, 'vc_pres': 2535.0,
           'vc_low': 334.0, 'vc_mid': 819.0, 'vc_air': 8000.0}


def default_state():
    return {'surround': False,
            'audio': {'mode': None, 'levels': {}},
            'mic': {'mode': None, 'levels': {}}}


def default_level(mode):
    return {'noise': 60, 'gate': 2}.get(mode, 55)


def level_of(side_state):
    mode = side_state.get('mode')
    return side_state.get('levels', {}).get(mode, default_level(mode))


# ── DSP model ──────────────────────────────────────────────────────────────

def _interp(l, pts):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if l <= x1 or (x1, y1) == pts[-1]:
            return y0 + (y1 - y0) * (l - x0) / (x1 - x0)
    return pts[0][1]


def _i3(l, a, b, c):
    return _interp(l, [(0.25, a), (0.55, b), (1.0, c)])


def _n3(l, a, b, c):
    return _interp(l, [(0.25, a), (0.60, b), (1.0, c)])


def params(side, mode, level):
    """Control values for every stage of one side. mode None makes every stage neutral."""
    chain = MIC_CHAIN if side == 'mic' else AUDIO_CHAIN
    p = {}

    def bq(name, freq, q, gain):
        p[name] = {'Freq': freq, 'Q': q, 'Gain': gain}

    def dyn(name, **kv):
        p[name].update({DYN_PORTS[k][0]: v for k, v in kv.items()})

    for name, kind in chain:
        if kind == DYN:
            p[name] = {port: default for port, default in DYN_PORTS.values()}
        else:
            bq(name, BQ_FREQ[name], 0.707, 0.0)
    if mode is None:
        return p

    l = level / 100.0
    if side == 'audio':
        if mode == 'bass':
            bq('bass', 105.0, 0.75, 9.0 * l)
        elif mode == 'clarity':
            bq('vc_shelf', _i3(l, 123.6, 169.1, 227.4), _i3(l, 0.995, 0.964, 0.914), -6.0)
            bq('vc_dip', _i3(l, 107.4, 151.1, 217.7), _i3(l, 1.187, 1.235, 1.299),
               _i3(l, -3.28, -3.78, -5.01))
            bq('vc_pres', 2535.0, _i3(l, 0.954, 1.002, 1.009), _i3(l, 1.538, 3.229, 6.142))
        elif mode == 'normalization':
            dyn('norm', gain=_i3(l, 6.5, 9.5, 16.0), comp_t=_i3(l, -2.75, -2.75, -5.25),
                comp_r=_i3(l, 100.0, 50.0, 6.0), knee=_i3(l, 6.0, 8.0, 8.0), attack=1.0,
                release=_i3(l, 200.0, 313.0, 500.0), window=10.0)
        dyn('lim', comp_t=-0.1, comp_r=100.0, attack=0.5, release=80.0, peak=1.0, window=20.0)
    else:
        if mode == 'noise':
            dyn('nc', exp_t=_n3(l, -47.5, -36.5, -19.5), exp_r=_n3(l, 2.5, 2.8, 3.2),
                floor=-60.0 if l < 0.45 else -200.0,
                mute=_n3(l, -92.0, -77.7, -61.6) if l >= 0.45 else -200.0,
                attack=4.0, release=_n3(l, 83.0, 117.0, 117.0), window=10.0)
        elif mode == 'gate':
            step = round(level)
            if step <= 1:
                dyn('gate', exp_t=-57.0, exp_r=2.0, floor=-32.0, attack=4.0, release=52.0)
            elif step == 2:
                dyn('gate', mute=-55.9, exp_t=-34.0, exp_r=2.1, attack=4.0, release=43.0)
            else:
                dyn('gate', mute=-33.5, exp_t=-6.5, exp_r=1.9, attack=4.0, release=43.0)
        elif mode == 'clarity':
            bq('vc_low', _i3(l, 308.0, 337.0, 184.0), _i3(l, 0.85, 1.55, 1.16),
               _i3(l, -7.35, -11.7, -17.0))
            bq('vc_mid', _i3(l, 532.0, 858.0, 1340.0), _i3(l, 0.122, 0.253, 0.569),
               _i3(l, 4.82, 8.7, 13.6))
            bq('vc_air', 8000.0, 0.707, _i3(l, 2.37, 1.38, 1.82))
        elif mode == 'normalization':
            dyn('norm', gain=_i3(l, 7.2, 11.9, 18.9), comp_t=_i3(l, -13.0, -13.0, -9.5),
                comp_r=_i3(l, 3.0, 8.0, 20.0), knee=_i3(l, 0.0, 0.0, 11.0), attack=5.0,
                release=300.0, window=10.0)
        dyn('lim', comp_t=-1.0, comp_r=100.0, attack=0.5, release=80.0, peak=1.0, window=20.0)
    return p


def _num(v):
    s = f'{v:.6f}'.rstrip('0').rstrip('.')
    return s if s not in ('', '-', '-0') else '0'


def _controls(values):
    return ' '.join(f'"{k}" = {_num(v)}' for k, v in values.items())


def _node(name, kind, values):
    if kind == DYN:
        head = f'type = ladspa name = {name} plugin = "{PLUGIN}" label = razer_dyn'
    else:
        head = f'type = builtin name = {name} label = {kind}'
    return f'{{ {head} control = {{ {_controls(values)} }} }}'


def _port(name, kind, output):
    if kind == DYN:
        return f'{name}:{"Output" if output else "Input"}'
    return f'{name}:{"Out" if output else "In"}'


def _game_graph(p, surround):
    """Explicit stereo graph: optional HRIR surround, then the enhancement chain per channel."""
    nodes, links, inputs, outputs = [], [], [], []
    for ch in 'LR':
        chain = [(f'{n}{ch}', k, p[n]) for n, k in AUDIO_CHAIN]
        nodes += [_node(n, k, v) for n, k, v in chain]
        links += [(_port(a, ka, True), _port(b, kb, False))
                  for (a, ka, _), (b, kb, _) in zip(chain, chain[1:])]
        outputs.append(_port(chain[-1][0], chain[-1][1], True))
        if not surround:
            inputs.append(_port(chain[0][0], chain[0][1], False))
    if surround:
        # HeSuVi channel layout: 0/1 = left speaker to left/right ear, 8/7 = right speaker.
        nodes += [
            '{ type = builtin name = copyL label = copy }',
            '{ type = builtin name = copyR label = copy }',
        ] + [f'{{ type = builtin name = {n} label = convolver config = {{ filename = "{HRIR}" channel = {c} }} }}'
             for n, c in (('convLL', 0), ('convLR', 1), ('convRR', 7), ('convRL', 8))] + [
            # Each pair sums two attenuated ear paths; 3.17 nets about +4.3 dB, kept under clipping.
            '{ type = builtin name = mixL label = mixer control = { "Gain 1" = 3.17 "Gain 2" = 3.17 } }',
            '{ type = builtin name = mixR label = mixer control = { "Gain 1" = 3.17 "Gain 2" = 3.17 } }',
            '{ type = builtin name = shelfL label = bq_highshelf control = { "Freq" = 2500 "Q" = 0.707 "Gain" = -2.5 } }',
            '{ type = builtin name = shelfR label = bq_highshelf control = { "Freq" = 2500 "Q" = 0.707 "Gain" = -2.5 } }',
        ]
        links += [('copyL:Out', 'convLL:In'), ('copyL:Out', 'convLR:In'),
                  ('copyR:Out', 'convRR:In'), ('copyR:Out', 'convRL:In'),
                  ('convLL:Out', 'mixL:In 1'), ('convRL:Out', 'mixL:In 2'),
                  ('convLR:Out', 'mixR:In 1'), ('convRR:Out', 'mixR:In 2'),
                  ('mixL:Out', 'shelfL:In'), ('mixR:Out', 'shelfR:In'),
                  ('shelfL:Out', _port('bassL', LOWSHELF, False)),
                  ('shelfR:Out', _port('bassR', LOWSHELF, False))]
        inputs = ['copyL:In', 'copyR:In']
    return nodes, links, inputs, outputs


def _mic_graph(p):
    chain = [(n, k, p[n]) for n, k in MIC_CHAIN]
    links = [(_port(a, ka, True), _port(b, kb, False))
             for (a, ka, _), (b, kb, _) in zip(chain, chain[1:])]
    return ([_node(n, k, v) for n, k, v in chain], links,
            [_port(chain[0][0], chain[0][1], False)], [_port(chain[-1][0], chain[-1][1], True)])


def _filter_module(description, graph, channels, capture, playback):
    nodes, links, inputs, outputs = graph
    position = '[ FL FR ]' if channels == 2 else '[ MONO ]'
    q = lambda xs: ' '.join(f'"{x}"' for x in xs)
    return f'''    {{ name = libpipewire-module-filter-chain
        flags = [ nofail ]
        args = {{
            node.description = "{description}"
            media.name = "{description}"
            audio.rate = 48000
            audio.channels = {channels}
            audio.position = {position}
            filter.graph = {{
                nodes = [
                    {(chr(10) + ' ' * 20).join(nodes)}
                ]
                links = [
                    {(chr(10) + ' ' * 20).join(f'{{ output = "{a}" input = "{b}" }}' for a, b in links)}
                ]
                inputs = [ {q(inputs)} ]
                outputs = [ {q(outputs)} ]
            }}
            capture.props = {{ {capture} }}
            playback.props = {{ {playback} }}
        }}
    }}'''


def config(state, targets):
    """The whole standalone PipeWire config for the current state and headset node names."""
    game, mic = targets
    a, m = state['audio'], state['mic']
    smart = 'filter.smart = true filter.smart.name = "{}" filter.smart.target = {{ node.name = "{}" }}'
    modules = []
    if game and (state['surround'] or a.get('mode')):
        modules.append(_filter_module(
            'BlackShark Effects', _game_graph(params('audio', a.get('mode'), level_of(a)),
                                             state['surround']), 2,
            f'node.name = "{GAME_NODES[0]}" media.class = Audio/Sink '
            + smart.format(GAME_NODES[0], game),
            f'node.name = "{GAME_NODES[1]}" node.passive = true node.dont-move = true media.role = "DSP" '
            'stream.dont-remix = true'))
    if mic and m.get('mode'):
        modules.append(_filter_module(
            'BlackShark Mic Effects', _mic_graph(params('mic', m['mode'], level_of(m))), 1,
            f'node.name = "{MIC_NODES[0]}" node.passive = true node.dont-move = true stream.dont-remix = true',
            f'node.name = "{MIC_NODES[1]}" media.class = Audio/Source '
            + smart.format(MIC_NODES[1], mic)))
    return f'''# Generated by blackctl; rewritten whenever an effect changes.
context.properties = {{ log.level = 0 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.* = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
{chr(10).join(modules)}
]
'''


# ── PipeWire plumbing ──────────────────────────────────────────────────────

def _nodes():
    try:
        out = subprocess.run(['pw-dump'], capture_output=True, timeout=5).stdout
        objs = json.loads(out or b'[]')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [(o['id'], o['info']['props']) for o in objs
            if o.get('type') == 'PipeWire:Interface:Node' and o.get('info', {}).get('props')]


def headset_targets():
    """(sink node.name, source node.name) of the connected headset, or None for each."""
    game = mic = None
    for _, props in _nodes():
        name = props.get('node.name', '')
        if HEADSET_NODE not in name:
            continue
        if props.get('media.class') == 'Audio/Sink' and (game is None or 'game' in name):
            game = name
        elif props.get('media.class') == 'Audio/Source':
            mic = name
    return game, mic


def _systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True).returncode == 0


def available():
    return os.path.exists(PLUGIN) and os.path.exists(HRIR)


def _push_live(side, values):
    """Change control values on a running chain without restarting it."""
    names = GAME_NODES if side == 'audio' else MIC_NODES
    channels = 'LR' if side == 'audio' else ('',)
    flat = []
    for stage, ctl in values.items():
        for ch in channels:
            flat += [f'"{stage}{ch}:{k}" {_num(v)}' for k, v in ctl.items()]
    pod = '{ params = [ ' + ' '.join(flat) + ' ] }'
    ids = [i for i, props in _nodes() if props.get('node.name') in names]
    for i in ids:
        subprocess.run(['pw-cli', 'set-param', str(i), 'Props', pod], capture_output=True)
    return bool(ids)


def apply(state, side=None):
    """Bring the running effects in line with state.

    side ('audio' or 'mic') says only that side's mode/level changed, which can be pushed
    live when its chain is already running. Anything else rewrites the config and restarts.
    Returns an error string, or None.
    """
    if not available():
        return 'effects are not installed (run install.sh again)'
    if not (state['surround'] or state['audio'].get('mode') or state['mic'].get('mode')):
        _systemctl('disable', '--now', SERVICE)
        return None
    targets = headset_targets()
    if None in targets:
        return 'connect the headset first'
    text = config(state, targets)
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(CONF) as f:
            old = f.read()
    except OSError:
        old = ''
    running = _systemctl('is-active', '--quiet', SERVICE)
    if text == old and running:
        return None
    with open(CONF, 'w') as f:
        f.write(text)

    if running and side and _structure(old) == _structure(text):
        values = params(side, state[side].get('mode'), level_of(state[side]))
        if _push_live(side, values):
            return None
    if not _systemctl('enable', SERVICE) or not _systemctl('restart', SERVICE):
        return f'could not start {SERVICE}'
    return None


def _structure(text):
    """Config with control values stripped: equal structures can be updated live."""
    return [line.split('control =')[0] for line in text.splitlines()]
