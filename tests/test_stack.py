from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text()


class StackTests(unittest.TestCase):
    def test_dkms_builds_the_two_modules(self):
        config = read("dkms.conf")
        self.assertIn('PACKAGE_NAME="blackctl"', config)
        self.assertIn('BUILT_MODULE_NAME[0]="razer-cfg255"', config)
        self.assertIn('BUILT_MODULE_NAME[1]="blackctl"', config)
        self.assertIn("obj-m := razer-cfg255.o blackctl.o", read("Makefile"))
        self.assertEqual({"cfg255.c", "blackctl.c"},
                         {p.name for p in (ROOT / "kernel").iterdir()})

    def test_driver_udev_and_app_agree_on_the_models(self):
        from blackctl.device import MODELS
        driver = read("kernel/blackctl.c")
        rules = read("data/99-blackctl.rules")
        self.assertEqual(len(MODELS), driver.count("HID_USB_DEVICE("))
        for pid in MODELS:
            self.assertIn(f"HID_USB_DEVICE(USB_VENDOR_ID_RAZER, 0x{pid.lower()})", driver)
            self.assertTrue(f"1532:{pid}" in rules or f"1532:057[679A]" in rules and pid.startswith("057"))
        self.assertIn('.name = "blackctl"', driver)

    def test_udev_rule_is_valid_and_uses_the_blackctl_driver(self):
        rules = read("data/99-blackctl.rules")
        self.assertIn("1532:0A55", rules)
        self.assertIn("057[679A]", rules)
        self.assertIn('DRIVER=="blackctl"', rules)
        self.assertNotIn("razerkraken", rules + read("data/blackctl-mount"))
        result = subprocess.run(["udevadm", "verify", str(ROOT / "data/99-blackctl.rules")],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_wireplumber_rule_selects_game_output_for_xbox(self):
        text = read("data/70-blackctl-blackshark-game-chat.conf")
        self.assertIn('device.product.id = "0x0a55"', text)
        self.assertIn('device.profile = "output:stereo-game+input:mono-chat"', text)
        self.assertIn("api.alsa.soft-mixer = true", text)

    def test_effects_service_runs_the_generated_config(self):
        unit = read("data/blackctl-fx.service")
        self.assertIn("ExecStart=/usr/bin/pipewire -c %S/blackctl/fx.conf", unit)
        self.assertIn("WantedBy=pipewire.service", unit)

    def test_installer_ships_every_data_file(self):
        script = read("install.sh")
        for path in ("dsp/razer_dyn.c", "dsp/kemar.wav", "data/blackctl-fx.service",
                     "data/blackctl-tray.service"):
            self.assertTrue((ROOT / path).is_file(), path)
            self.assertIn(path.split("/")[-1], script)
        result = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
