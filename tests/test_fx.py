import json
import unittest
from unittest import mock

from blackctl import device, fx

TARGETS = ("alsa_output.headset", "alsa_input.headset")


class EffectsTests(unittest.TestCase):
    def test_off_mode_is_neutral(self):
        for side, chain in (("audio", fx.AUDIO_CHAIN), ("mic", fx.MIC_CHAIN)):
            p = fx.params(side, None, 55)
            self.assertEqual({name for name, _ in chain}, set(p))
            for name, kind in chain:
                if kind == fx.DYN:
                    self.assertEqual(1.0, p[name]["Compressor ratio"])
                    self.assertEqual(1.0, p[name]["Expander ratio"])
                else:
                    self.assertEqual(0.0, p[name]["Gain"])

    def test_bass_boost_scales_with_level(self):
        self.assertAlmostEqual(9.0, fx.params("audio", "bass", 100)["bass"]["Gain"])
        self.assertAlmostEqual(4.5, fx.params("audio", "bass", 50)["bass"]["Gain"])

    def test_voice_gate_has_three_steps(self):
        gates = [fx.params("mic", "gate", step)["gate"] for step in (1, 2, 3)]
        self.assertEqual(3, len({g["Expander threshold (dB)"] for g in gates}))

    def test_config_contains_only_enabled_chains(self):
        state = fx.default_state()
        self.assertNotIn("filter-chain", fx.config(state, TARGETS))
        state["audio"]["mode"] = "bass"
        text = fx.config(state, TARGETS)
        self.assertIn('filter.smart.target = { node.name = "alsa_output.headset" }', text)
        self.assertNotIn("convolver", text)
        self.assertNotIn("blackctl.mic", text)
        state["surround"] = True
        state["mic"]["mode"] = "noise"
        text = fx.config(state, TARGETS)
        self.assertEqual(4, text.count("label = convolver"))
        self.assertIn('filter.smart.target = { node.name = "alsa_input.headset" }', text)

    def test_filter_streams_ignore_moves_from_other_apps(self):
        state = fx.default_state()
        state["audio"]["mode"] = "bass"
        state["mic"]["mode"] = "gate"
        self.assertEqual(2, fx.config(state, TARGETS).count("node.dont-move = true"))

    def test_level_change_keeps_structure_so_it_applies_live(self):
        state = fx.default_state()
        state["audio"]["mode"] = "bass"
        before = fx.config(state, TARGETS)
        state["audio"]["levels"] = {"bass": 90}
        after = fx.config(state, TARGETS)
        self.assertNotEqual(before, after)
        self.assertEqual(fx._structure(before), fx._structure(after))


class BatteryTests(unittest.TestCase):
    def test_waybar_output(self):
        with mock.patch.object(device, "battery", return_value=("Headset", 12, False)):
            out = json.loads(device.battery_text(as_json=True))
        self.assertEqual("12%", out["text"])
        self.assertEqual("low", out["class"])
        with mock.patch.object(device, "battery", return_value=None):
            self.assertEqual("", json.loads(device.battery_text(as_json=True))["text"])

    def test_plain_output_for_other_bars(self):
        with mock.patch.object(device, "battery", return_value=("Headset", 80, True)):
            self.assertEqual("80% +", device.battery_text())
        with mock.patch.object(device, "battery", return_value=None):
            self.assertEqual("", device.battery_text())

    def test_model_comes_from_the_usb_id(self):
        dev = device.Device("/sys/bus/hid/drivers/blackctl/0003:1532:057A.0004")
        self.assertEqual(("057A", "v3"), (dev.pid, dev.family))


if __name__ == "__main__":
    unittest.main()
