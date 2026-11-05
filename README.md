# blackctl

Linux driver and app for the Razer BlackShark V3 headsets.

I have the BlackShark V3 Pro for Xbox. Audio works on Linux out of the box, but that's it: no EQ,
no noise cancelling settings, no battery. So I wrote this.

What you get:

- EQ presets and a custom EQ
- THX, noise cancelling, sidetone, the function button, voice prompts, sleep timer
- Battery level in your system tray
- Optional software effects: virtual surround, bass boost, voice clarity, loudness
  normalization, and mic noise cancellation / voice gate
- The headset is set back to full hardware volume every time it reconnects

## Supported headsets

| Headset | USB ID | Status |
| --- | --- | --- |
| BlackShark V3 Pro for Xbox (dongle) | 1532:0a55 | Works, it's what I use |
| BlackShark V3 Pro (dongle / USB-C) | 1532:0577 / 0576 | Experimental |
| BlackShark V3 (dongle / USB-C) | 1532:057a / 0579 | Experimental |

I can't test the experimental ones. If you own one, please open an issue and tell me what works.

## Install

You need DKMS with your kernel headers, GTK 4 with PyGObject, PipeWire with WirePlumber 0.5+,
and the LADSPA header (`ladspa` on Arch, `ladspa-sdk` on Debian/Ubuntu).

```sh
git clone https://github.com/iiKovu/blackctl.git
cd blackctl
sudo ./install.sh
```

Unplug the headset or dongle for a few seconds, plug it back in, and run `blackctl`.
If the installer added you to the `plugdev` group, log out and back in once.

To remove it: `sudo ./install.sh --uninstall`. Your settings stay in `~/.config/blackctl.json`.

## Tray icon

The installer also starts `blackctl tray`, a small tray icon with the battery level that opens the
app when you click it. It shows up in any tray, Wayland or X11 (waybar, Quickshell, polybar, KDE, ...),
and only while the headset is connected. Turn it off with
`systemctl --user disable --now blackctl-tray`.

For a status bar module instead, `blackctl battery` prints the level (`--json` for waybar).

## How it works

There are two small kernel modules. `razer_cfg255` makes the headset turn on its settings
channel when it's plugged in (it only does that for Windows by default), and `blackctl` is the
driver that talks to it. Every setting is a file in `/sys/bus/hid/drivers/blackctl/`, so you can
also script it.

The effects run as a separate PipeWire process (`blackctl-fx.service`) that sits in front of the
headset. It only touches the headset, doesn't change your default devices, and stops when you turn
every effect off.

## License

GPL-2.0-or-later. Not affiliated with Razer.
