// SPDX-License-Identifier: GPL-2.0
/*
 * Written by Charles Daoust (git.forked.io/charles/razer-blackshark-v3-quirk).
 *
 * Arm the Razer BlackShark V3 family on stock kernels: BlackShark V3 Pro
 * (wired 1532:0576, wireless dongle 1532:0577), BlackShark V3 (wired
 * 1532:0579, wireless dongle 1532:057a), and BlackShark V3 for Xbox
 * (headset 1532:0a4e, wireless dongle 1532:0a55).
 *
 * The firmware decides at the FIRST enumeration after power-on
 * whether to enable its vendor/HID command channel, keyed on the wLength
 * of the host's first GET_DESCRIPTOR(CONFIGURATION) request: 255 (as
 * Windows sends) => enabled; 9 (Linux's header-only read) => the vendor
 * protocol handler is disabled for the rest of the power session (audio
 * still works; battery/EQ/all vendor commands are silently ignored).
 *
 * This module widens that one read for this one device via a kretprobe
 * on usb_get_descriptor(): entry handler swaps the 9-byte buffer for a
 * 256-byte scratch buffer and sets size=255; return handler copies the
 * 9 header bytes back. No sleeping, no wire behavior change beyond the
 * single read length. Interim fix until the usbcore quirk lands upstream.
 */
#include <linux/module.h>
#include <linux/kprobes.h>
#include <linux/usb.h>
#include <linux/slab.h>

struct c255_data {
	void *orig_buf;
	u8   *scratch;
};

static int hits;

static bool c255_match_pid(u16 pid)
{
	switch (pid) {
	case 0x0576:	/* BlackShark V3 Pro, wired */
	case 0x0577:	/* BlackShark V3 Pro, wireless dongle */
	case 0x0579:	/* BlackShark V3, wired */
	case 0x057a:	/* BlackShark V3, wireless dongle */
	case 0x0a4e:	/* BlackShark V3 for Xbox, headset */
	case 0x0a55:	/* BlackShark V3 for Xbox, wireless dongle */
		return true;
	}
	return false;
}

static int c255_entry(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct usb_device *dev = (struct usb_device *)regs->di;
	struct c255_data *d = (struct c255_data *)ri->data;
	u16 pid;

	d->orig_buf = NULL;
	d->scratch = NULL;

	if (!dev || le16_to_cpu(dev->descriptor.idVendor) != 0x1532)
		return 0;
	pid = le16_to_cpu(dev->descriptor.idProduct);
	if (!c255_match_pid(pid))
		return 0;
	if ((u8)regs->si != USB_DT_CONFIG || (int)regs->r8 != 9)
		return 0;

	d->scratch = kmalloc(256, GFP_ATOMIC);
	if (!d->scratch)
		return 0;

	d->orig_buf = (void *)regs->cx;
	regs->cx = (unsigned long)d->scratch;
	regs->r8 = 255;
	hits++;
	pr_info("razer-cfg255: widened first config read for 1532:%04x (#%d)\n",
		pid, hits);
	return 0;
}

static int c255_ret(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct c255_data *d = (struct c255_data *)ri->data;

	if (d->orig_buf) {
		memcpy(d->orig_buf, d->scratch, 9);
		kfree(d->scratch);
	}
	return 0;
}

static struct kretprobe c255_krp = {
	.kp.symbol_name = "usb_get_descriptor",
	.entry_handler  = c255_entry,
	.handler        = c255_ret,
	.data_size      = sizeof(struct c255_data),
	.maxactive      = 8,
};

static int __init c255_init(void)
{
	int ret = register_kretprobe(&c255_krp);

	if (ret) {
		pr_err("razer-cfg255: register_kretprobe failed: %d\n", ret);
		return ret;
	}
	pr_info("razer-cfg255: armed on usb_get_descriptor\n");
	return 0;
}

static void __exit c255_exit(void)
{
	unregister_kretprobe(&c255_krp);
	pr_info("razer-cfg255: removed (%d widened reads)\n", hits);
}

module_init(c255_init);
module_exit(c255_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Widen first config-descriptor read for Razer BlackShark V3 family");
MODULE_AUTHOR("Charles Daoust");
