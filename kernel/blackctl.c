// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * blackctl: control driver for the Razer BlackShark V3 family.
 * Based on OpenRazer's razerkraken driver, Copyright (c) 2015 Terri Cain and contributors.
 *
 *   1532:0a55  BlackShark V3 Pro for Xbox (dongle)   tested
 *   1532:0577  BlackShark V3 Pro (dongle)            experimental
 *   1532:0576  BlackShark V3 Pro (USB-C)             experimental
 *   1532:057a  BlackShark V3 (dongle)                experimental
 *   1532:0579  BlackShark V3 (USB-C)                 experimental
 *
 * The vendor channel only answers once razer_cfg255 has made the first
 * configuration read look like Windows' (see cfg255.c).
 *
 * Settings are sysfs attributes on the HID device. Only the ones a model
 * supports are created:
 *   battery        percent 0..100, -1 until the headset reports it
 *   charging       1 / 0, -1 unknown
 *   sidetone       0..15
 *   thx            1 = THX spatial audio, 0 = stereo
 *   anc            "<mode> <level>": mode 0 off / 1 ANC / 2 ambient (V3 Pro), level 1..4
 *   power_save     minutes before sleep: 0 (never), 15, 30, 45, 60
 *   eq             write "<slot>" to select a stored preset, or "<slot> b0 .. b9"
 *                  (-6..+6 dB) to write bands: slot 5 on Xbox, 0..4 on V3.
 *                  Reads the active slot.
 *   fn_button      0 game/chat, 1 sidetone, 2 footsteps, 3 bluetooth volume
 *   audio_prompts  1 / 0
 *   ull            ultra-low latency 1 / 0
 *   game_chat      game/chat balance 0..20, 10 = centre
 * Reads are cache-only: values come from writes and from the headset's own
 * pushes (on-board buttons), so polling never disturbs the wireless link.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/hid.h>
#include <linux/usb.h>
#include <linux/usb/audio.h>
#include <linux/completion.h>
#include <linux/workqueue.h>
#include <linux/version.h>

#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 17, 0)
#define system_percpu_wq system_wq
#endif

#define USB_VENDOR_ID_RAZER     0x1532
#define REPORT_LEN              64

enum family { XBOX, V3PRO, V3 };

#define HAS_POWER_SAVE  BIT(0)
#define HAS_ANC         BIT(1)
#define HAS_AMBIENT     BIT(2)
#define HAS_ULL         BIT(3)
#define HAS_GAME_CHAT   BIT(4)

struct model {
    u16 pid;
    enum family family;
    bool wireless;
    unsigned int features;
};

static const struct model models[] = {
    { 0x0a55, XBOX,  true,  HAS_POWER_SAVE | HAS_ANC },
    { 0x0577, V3PRO, true,  HAS_POWER_SAVE | HAS_ANC | HAS_AMBIENT | HAS_ULL | HAS_GAME_CHAT },
    { 0x0576, V3PRO, false, HAS_ANC | HAS_AMBIENT | HAS_GAME_CHAT },
    { 0x057a, V3,    true,  HAS_POWER_SAVE | HAS_ULL | HAS_GAME_CHAT },
    { 0x0579, V3,    false, HAS_GAME_CHAT },
};

/* Command classes (byte 10). Pushes and GET replies use the class with bit 7 clear. */
#define CLS_BATTERY             0x21
#define CLS_CHARGING            0x2a
#define CLS_LINK                0x20    /* push: RF link up (data[13] == 1) */
#define CLS_SIDETONE_PUSH       0x19
#define CLS_SIDETONE_INIT       0x98
#define CLS_SIDETONE            0x99
#define CLS_THX                 0x9e    /* Xbox: 0 = THX, 1 = stereo */
#define CLS_ANC                 0x92
#define CLS_ANC_PUSH            0x12
#define CLS_POWER_SAVE          0xac
#define CLS_ULL                 0xdf
#define CLS_ULL_PUSH            0x5f
#define CLS_GAME_CHAT           0xdc
#define CLS_GAME_CHAT_PUSH      0x5c
#define CLS_GAME_CHAT_PUSH_V3   0x65
#define CLS_PROMPTS             0xe5
#define CLS_PROMPTS_PUSH        0x66
#define CLS_FN_BUTTON           0xea
#define CLS_FN_BUTTON_PUSH      0x6a
#define CLS_EQ_STATE            0xe1
#define CLS_EQ_BANDS            0x95
#define CLS_EQ_META             0xe0
#define CLS_EQ_COMMIT           0xeb
#define CLS_EQ_SLOT_PUSH        0x60

#define ANC_WIRE_OFF            0x00
#define ANC_WIRE_ON             0x01
#define ANC_WIRE_AMBIENT        0x50
#define EQ_SLOTS                9
#define EQ_XBOX_CUSTOM_SLOT     5

struct blackctl_device {
    struct hid_device *hdev;
    struct usb_device *usb_dev;
    const struct model *model;
    struct mutex lock;
    struct completion response;

    struct urb *intr_urb;
    u8 *intr_buf;
    dma_addr_t intr_dma;
    atomic_t eproto_count;
    struct work_struct intr_recover_work;
    struct work_struct full_power_work;
    struct delayed_work battery_work;
    struct delayed_work rf_wake_work;
    bool attrs_created;
    bool opened;

    int battery;
    s8 charging;
    s8 sidetone;
    s8 thx;
    s8 anc_mode;
    s8 anc_level;
    s8 power_save;
    s8 eq_slot;
    s8 fn_button;
    s8 audio_prompts;
    s8 ull;
    s8 game_chat;
};

/*
 * Vendor packet: report 2, transaction 0x60, data size, flags, class, command
 * id, arguments, XOR CRC of bytes 0..61. The Xbox firmware counts the data
 * size without the flags byte and is always addressed as wireless (0x80); the
 * PC models count it and use 0x00 over USB-C.
 */
static void blackctl_build(const struct blackctl_device *dev, u8 *buf, u8 cls, u8 cmd,
                           const u8 *args, u8 len)
{
    u8 crc = 0;
    int i;

    memset(buf, 0, REPORT_LEN);
    buf[0] = 0x02;
    buf[2] = 0x60;
    buf[6] = (dev->model->family == XBOX ? 3 : 4) + len;
    buf[9] = dev->model->wireless || dev->model->family != V3 ? 0x80 : 0x00;
    buf[10] = cls;
    buf[12] = cmd;
    if (len)
        memcpy(&buf[13], args, len);
    for (i = 0; i < 62; i++)
        crc ^= buf[i];
    buf[62] = crc;
}

/* Send one command and wait up to 1 s for its reply. Caller holds lock. */
static int blackctl_send(struct blackctl_device *dev, u8 cls, u8 cmd, const u8 *args, u8 len)
{
    u8 *buf;
    int ret;

    buf = kmalloc(REPORT_LEN, GFP_KERNEL);
    if (!buf)
        return -ENOMEM;
    blackctl_build(dev, buf, cls, cmd, args, len);
    reinit_completion(&dev->response);
    ret = hid_hw_raw_request(dev->hdev, 0x02, buf, REPORT_LEN,
                             HID_OUTPUT_REPORT, HID_REQ_SET_REPORT);
    kfree(buf);
    if (ret != REPORT_LEN) {
        hid_warn(dev->hdev, "command %02x failed: %d\n", cls, ret);
        return -EIO;
    }
    if (!wait_for_completion_timeout(&dev->response, msecs_to_jiffies(1000)))
        return -ETIMEDOUT;
    return 0;
}

static int blackctl_send_locked(struct blackctl_device *dev, u8 cls, u8 cmd, const u8 *args, u8 len)
{
    int ret;

    mutex_lock(&dev->lock);
    ret = blackctl_send(dev, cls, cmd, args, len);
    mutex_unlock(&dev->lock);
    return ret;
}

/* Single-value setting: PC models take [value], the V3 Pro and Xbox [value, 0]. */
static void blackctl_set(struct blackctl_device *dev, u8 cls, u8 value)
{
    u8 args[2] = { value, 0x00 };

    blackctl_send_locked(dev, cls, 0x01, args, dev->model->family == V3 ? 1 : 2);
}

static inline u8 gain_encode(int v)
{
    return v < 0 ? (u8)(0x80 | -v) : (u8)v;
}

/* V3 Pro / Xbox factory presets 0..4 (Default, Game, Movie, Music, Esports). */
static const u8 eq_bands[5][12] = {
    { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },
    { 0x01, 0x02, 0x02, 0x05, 0x05, 0x01, 0x81, 0x02, 0x03, 0x03, 0x03, 0x00 },
    { 0x02, 0x03, 0x03, 0x03, 0x81, 0x84, 0x84, 0x02, 0x03, 0x03, 0x03, 0x00 },
    { 0x03, 0x02, 0x02, 0x00, 0x00, 0x01, 0x81, 0x81, 0x03, 0x03, 0x03, 0x00 },
    { 0x04, 0x01, 0x01, 0x81, 0x00, 0x02, 0x00, 0x04, 0x04, 0x04, 0x83, 0x00 },
};
static const u8 eq_meta[5][7] = {
    { 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00 },
    { 0x01, 0x01, 0x01, 0x00, 0x01, 0x00, 0x00 },
    { 0x02, 0x03, 0x01, 0x00, 0x03, 0x00, 0x00 },
    { 0x03, 0x02, 0x01, 0x00, 0x02, 0x00, 0x00 },
    { 0x04, 0x04, 0x01, 0x00, 0x0b, 0x00, 0x00 },
};
static const u8 eq_commit[5][12] = {
    { 0x00, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },
    { 0x01, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },
    { 0x02, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },
    { 0x03, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 },
    { 0x04, 0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00 },
};
/* V3 apply-command bytes per profile: [14], [17], [18]. */
static const u8 eq_v3_apply[5][3] = {
    { 0x00, 0x00, 0x01 }, { 0x01, 0x01, 0x01 }, { 0x03, 0x03, 0x01 },
    { 0x02, 0x02, 0x01 }, { 0x04, 0x0a, 0x00 },
};

/*
 * Select (and optionally write) an EQ slot with the 5-command sequence
 * captured from Synapse: state, bands, metadata, apply, commit. Some frames
 * are never answered, so a reply timeout is not an error. Caller holds lock.
 */
static int blackctl_apply_eq(struct blackctl_device *dev, u8 slot, const s8 *custom)
{
    u8 state[2] = { 0x01, 0x00 }, bands[12], meta[7], commit[12];
    u8 row = slot < 5 && !custom ? slot : 0;
    bool v3 = dev->model->family == V3;
    int i, ret = 0;

    if (v3) {
        /* The V3 always sends bands; its metadata frame carries the apply bytes. */
        memset(bands, 0, sizeof(bands));
        memset(meta, 0, sizeof(meta));
        memset(commit, 0, sizeof(commit));
        meta[1] = eq_v3_apply[slot][0];
        meta[2] = meta[3] = 0x01;
        meta[4] = eq_v3_apply[slot][1];
        meta[5] = eq_v3_apply[slot][2];
        commit[3] = commit[4] = 0x01;
    } else {
        memcpy(bands, eq_bands[row], sizeof(bands));
        memcpy(meta, eq_meta[row], sizeof(meta));
        memcpy(commit, eq_commit[row], sizeof(commit));
        if (slot >= 5 || custom)
            meta[1] = meta[4] = slot;   /* also names the spoken prompt */
        if (dev->model->family == XBOX)
            meta[3] = 0x01;             /* marks the slot being selected */
    }
    bands[0] = meta[0] = commit[0] = slot;
    if (custom)
        for (i = 0; i < 10; i++)
            bands[i + 1] = gain_encode(custom[i]);

    for (i = 0; i < 5 && (ret >= 0 || ret == -ETIMEDOUT); i++) {
        switch (i) {
        case 0:
        case 3:
            state[0] = i ? 0x02 : 0x01;
            ret = blackctl_send(dev, CLS_EQ_STATE, 0x01, state, v3 ? 1 : 2);
            break;
        case 1:
            ret = blackctl_send(dev, CLS_EQ_BANDS, 0x0b, bands, v3 ? 11 : 12);
            break;
        case 2:
            ret = blackctl_send(dev, CLS_EQ_META, 0x06, meta, v3 ? 6 : 7);
            break;
        case 4:
            ret = blackctl_send(dev, CLS_EQ_COMMIT, 0x0b, commit, v3 ? 11 : 12);
            break;
        }
    }
    return ret == -ETIMEDOUT ? 0 : ret;
}

/* ── sysfs ─────────────────────────────────────────────────────────────── */

#define SHOW_INT(name)                                                                \
static ssize_t name##_show(struct device *d, struct device_attribute *a, char *buf)   \
{                                                                                     \
    struct blackctl_device *dev = dev_get_drvdata(d);                                 \
    return sysfs_emit(buf, "%d\n", (int)dev->name);                                   \
}

SHOW_INT(battery)
SHOW_INT(charging)
SHOW_INT(sidetone)
SHOW_INT(thx)
SHOW_INT(power_save)
SHOW_INT(fn_button)
SHOW_INT(audio_prompts)
SHOW_INT(ull)
SHOW_INT(game_chat)

static ssize_t eq_show(struct device *d, struct device_attribute *a, char *buf)
{
    struct blackctl_device *dev = dev_get_drvdata(d);

    return sysfs_emit(buf, "%d\n", dev->eq_slot);
}

static ssize_t anc_show(struct device *d, struct device_attribute *a, char *buf)
{
    struct blackctl_device *dev = dev_get_drvdata(d);

    return sysfs_emit(buf, "%d %d\n", dev->anc_mode, dev->anc_level);
}

static int parse_uint(const char *buf, unsigned int max, unsigned int *val)
{
    if (kstrtouint(buf, 10, val))
        return -EINVAL;
    *val = min(*val, max);
    return 0;
}

static ssize_t sidetone_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int val;

    if (parse_uint(buf, 15, &val))
        return -EINVAL;
    if (dev->model->family == V3)
        blackctl_set(dev, CLS_SIDETONE_INIT, 0x01);
    blackctl_set(dev, CLS_SIDETONE, val);
    dev->sidetone = val;
    return count;
}

static ssize_t thx_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int val;

    if (parse_uint(buf, 1, &val))
        return -EINVAL;
    blackctl_set(dev, CLS_THX, dev->model->family == XBOX ? !val : val);
    dev->thx = val;
    return count;
}

static ssize_t anc_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int mode, level;
    u8 args[3];

    if (sscanf(buf, "%u %u", &mode, &level) != 2)
        return -EINVAL;
    if (mode > 2 || (mode == 2 && !(dev->model->features & HAS_AMBIENT)))
        mode = 0;
    level = clamp(level, 1U, 4U);
    args[0] = mode == 1 ? ANC_WIRE_ON : mode == 2 ? ANC_WIRE_AMBIENT : ANC_WIRE_OFF;
    args[1] = level;
    args[2] = 0x00;
    blackctl_send_locked(dev, CLS_ANC, 0x02, args, dev->model->family == XBOX ? 3 : 2);
    dev->anc_mode = mode;
    dev->anc_level = level;
    return count;
}

static ssize_t power_save_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int val;

    if (parse_uint(buf, 60, &val))
        return -EINVAL;
    if (dev->model->family == V3)
        val = max(roundup(val, 15U), 15U);  /* the V3 has no "never" */
    blackctl_set(dev, CLS_POWER_SAVE, val);
    dev->power_save = val;
    return count;
}

static ssize_t eq_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    enum family family = dev->model->family;
    int v[11], i, n, ret;
    s8 custom[10];

    n = sscanf(buf, "%d %d %d %d %d %d %d %d %d %d %d", &v[0], &v[1], &v[2], &v[3],
               &v[4], &v[5], &v[6], &v[7], &v[8], &v[9], &v[10]);
    if (n < 1 || v[0] < 0 || v[0] >= (family == V3 ? 5 : EQ_SLOTS))
        return -EINVAL;
    if (n == 11) {
        if ((family == XBOX && v[0] != EQ_XBOX_CUSTOM_SLOT) || family == V3PRO)
            return -EINVAL;
        for (i = 0; i < 10; i++) {
            if (v[i + 1] < -6 || v[i + 1] > 6)
                return -EINVAL;
            custom[i] = v[i + 1];
        }
    } else if (n != 1 || family == V3) {
        return -EINVAL;     /* the V3 stores no presets: always send bands */
    }

    mutex_lock(&dev->lock);
    ret = blackctl_apply_eq(dev, v[0], n == 11 ? custom : NULL);
    if (!ret)
        dev->eq_slot = v[0];
    mutex_unlock(&dev->lock);
    return ret ? ret : count;
}

static ssize_t fn_button_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int val;

    if (kstrtouint(buf, 10, &val) || val > 3)
        return -EINVAL;
    /* Same five-byte frame on every model: the Xbox counts one byte less. */
    blackctl_send_locked(dev, CLS_FN_BUTTON, 0x01, (u8[]){ val, 0x00 },
                         dev->model->family == XBOX ? 2 : 1);
    dev->fn_button = val;
    return count;
}

static ssize_t audio_prompts_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int val;

    if (parse_uint(buf, 1, &val))
        return -EINVAL;
    blackctl_send_locked(dev, CLS_PROMPTS, 0x02, (u8[]){ 0x00, val, 0x00 }, 3);
    dev->audio_prompts = val;
    return count;
}

static ssize_t ull_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int val;

    if (parse_uint(buf, 1, &val))
        return -EINVAL;
    blackctl_set(dev, CLS_ULL, val);
    dev->ull = val;
    return count;
}

static ssize_t game_chat_store(struct device *d, struct device_attribute *a, const char *buf, size_t count)
{
    struct blackctl_device *dev = dev_get_drvdata(d);
    unsigned int val;

    if (parse_uint(buf, 20, &val))
        return -EINVAL;
    blackctl_send_locked(dev, CLS_GAME_CHAT, 0x01, (u8[]){ val, 0x00 }, 2);
    dev->game_chat = val;
    return count;
}

static DEVICE_ATTR_RO(battery);
static DEVICE_ATTR_RO(charging);
static DEVICE_ATTR_RW(sidetone);
static DEVICE_ATTR_RW(thx);
static DEVICE_ATTR_RW(anc);
static DEVICE_ATTR_RW(power_save);
static DEVICE_ATTR_RW(eq);
static DEVICE_ATTR_RW(fn_button);
static DEVICE_ATTR_RW(audio_prompts);
static DEVICE_ATTR_RW(ull);
static DEVICE_ATTR_RW(game_chat);

static struct attribute *blackctl_attrs[] = {
    &dev_attr_battery.attr,
    &dev_attr_charging.attr,
    &dev_attr_sidetone.attr,
    &dev_attr_thx.attr,
    &dev_attr_anc.attr,
    &dev_attr_power_save.attr,
    &dev_attr_eq.attr,
    &dev_attr_fn_button.attr,
    &dev_attr_audio_prompts.attr,
    &dev_attr_ull.attr,
    &dev_attr_game_chat.attr,
    NULL
};

static umode_t blackctl_attr_visible(struct kobject *kobj, struct attribute *attr, int n)
{
    struct blackctl_device *dev = dev_get_drvdata(kobj_to_dev(kobj));
    unsigned int features = dev->model->features;

    if ((attr == &dev_attr_power_save.attr && !(features & HAS_POWER_SAVE)) ||
        (attr == &dev_attr_anc.attr && !(features & HAS_ANC)) ||
        (attr == &dev_attr_ull.attr && !(features & HAS_ULL)) ||
        (attr == &dev_attr_game_chat.attr && !(features & HAS_GAME_CHAT)))
        return 0;
    return attr->mode;
}

static const struct attribute_group blackctl_group = {
    .attrs = blackctl_attrs,
    .is_visible = blackctl_attr_visible,
};

/* ── headset pushes and replies ────────────────────────────────────────── */

/*
 * Cache one vendor frame (replies and on-board button pushes) and wake the
 * waiting command. Xbox frames mark success in data[1] and put the payload
 * size in data[6]; PC frames mark it in data[11] and size it in data[12].
 * Payload bytes start at data[13]. Runs in interrupt context: must not sleep.
 */
static void blackctl_cache(struct blackctl_device *dev, const u8 *data)
{
    bool xbox = dev->model->family == XBOX;
    u8 payload_len;

    if (xbox ? data[1] != 0x02 || data[6] < 4 : (data[11] != 0x01 && data[11] != 0x02) || !data[12])
        goto done;
    payload_len = xbox ? data[6] - 3 : data[12];

    switch (data[10]) {
    case CLS_BATTERY:
        if (data[13] <= 100)
            dev->battery = data[13];
        if (payload_len >= 2 && data[14] <= 1)
            dev->charging = data[14];
        break;
    case CLS_CHARGING:
        if (data[13] <= 1)
            dev->charging = data[13];
        break;
    case CLS_LINK:
        /* RF link re-established: the battery may be stale. The Xbox headset
         * also powers up at reduced hardware volume. */
        if (data[13] == 0x01) {
            dev->charging = -1;
            dev->battery = -1;
            if (xbox) {
                schedule_work(&dev->full_power_work);
                mod_delayed_work(system_percpu_wq, &dev->battery_work, msecs_to_jiffies(250));
            }
        }
        break;
    case CLS_SIDETONE_PUSH:
        if (data[13] <= 15)
            dev->sidetone = data[13];
        break;
    case CLS_PROMPTS_PUSH:
        if (data[13] <= 1)
            dev->audio_prompts = data[13];
        break;
    case CLS_FN_BUTTON_PUSH:
        if (data[13] <= 3)
            dev->fn_button = data[13];
        break;
    case CLS_THX:
        if (data[13] <= 1)
            dev->thx = xbox ? !data[13] : data[13];
        break;
    case CLS_ULL_PUSH:
        if (data[13] <= 1)
            dev->ull = data[13];
        break;
    case CLS_GAME_CHAT_PUSH:
    case CLS_GAME_CHAT_PUSH_V3:
        if (data[13] <= 20)
            dev->game_chat = data[13];
        break;
    case CLS_ANC_PUSH:
        if (data[13] == ANC_WIRE_OFF || data[13] == ANC_WIRE_ON)
            dev->anc_mode = data[13];
        else if (data[13] == ANC_WIRE_AMBIENT)
            dev->anc_mode = 2;
        if (data[14] >= 1 && data[14] <= 4)
            dev->anc_level = data[14];
        break;
    case CLS_EQ_SLOT_PUSH:
        if (data[13] < EQ_SLOTS)
            dev->eq_slot = data[13];
        break;
    }
done:
    complete(&dev->response);
}

/* Xbox: restore the USB audio feature units to 0 dB (full hardware volume). */
static void blackctl_full_power_work(struct work_struct *work)
{
    static const struct { u8 interface, unit; } targets[] = {
        { 0, 2 },  /* chat playback */
        { 3, 10 }, /* game playback */
        { 0, 6 },  /* microphone */
    };
    struct blackctl_device *dev = container_of(work, struct blackctl_device, full_power_work);
    __le16 zero_db = cpu_to_le16(0);
    int i, ret;

    for (i = 0; i < ARRAY_SIZE(targets); i++) {
        ret = usb_control_msg_send(dev->usb_dev, 0, UAC_SET_CUR,
                                   USB_DIR_OUT | USB_TYPE_CLASS | USB_RECIP_INTERFACE,
                                   UAC_FU_VOLUME << 8,
                                   (targets[i].unit << 8) | targets[i].interface,
                                   &zero_db, sizeof(zero_db), USB_CTRL_SET_TIMEOUT, GFP_KERNEL);
        if (ret)
            hid_warn(dev->hdev, "could not restore feature unit %u to 0 dB: %d\n",
                     targets[i].unit, ret);
    }
}

static void blackctl_query_battery(struct blackctl_device *dev)
{
    blackctl_send_locked(dev, CLS_BATTERY, 0x00, (u8[]){ 0x00 }, 1);
}

/* Xbox: poll battery every 5 s until the headset answers (the RF link can take a while). */
static void blackctl_battery_work(struct work_struct *work)
{
    struct blackctl_device *dev = container_of(work, struct blackctl_device, battery_work.work);

    if (dev->battery >= 0)
        return;
    blackctl_query_battery(dev);
    if (dev->battery < 0)
        mod_delayed_work(system_percpu_wq, &dev->battery_work, msecs_to_jiffies(5000));
}

/* PC V3 Pro: the dongle drops every query until Synapse's three primers are sent. */
static void blackctl_handshake(struct blackctl_device *dev)
{
    static const struct { u8 cls, flags; } primers[] = { { 0x02, 0x00 }, { 0x2a, 0x00 }, { 0x2a, 0x80 } };
    u8 *buf = kmalloc(REPORT_LEN, GFP_KERNEL);
    int i, j;

    if (!buf)
        return;
    for (i = 0; i < ARRAY_SIZE(primers); i++) {
        blackctl_build(dev, buf, primers[i].cls, 0x00, NULL, 0);
        buf[9] = primers[i].flags;
        buf[62] = 0;
        for (j = 0; j < 62; j++)
            buf[62] ^= buf[j];
        hid_hw_raw_request(dev->hdev, 0x02, buf, REPORT_LEN, HID_OUTPUT_REPORT, HID_REQ_SET_REPORT);
        usleep_range(2000, 3000);
    }
    kfree(buf);
}

/* Output report 5 [0x05, 0x00] every 3.5 s keeps the RF telemetry channel awake. */
static void blackctl_rf_wake_work(struct work_struct *work)
{
    struct blackctl_device *dev = container_of(work, struct blackctl_device, rf_wake_work.work);
    struct usb_interface *intf = to_usb_interface(dev->hdev->dev.parent);
    u8 *report = kmalloc(2, GFP_KERNEL);

    if (report) {
        report[0] = 0x05;
        report[1] = 0x00;
        usb_control_msg(dev->usb_dev, usb_sndctrlpipe(dev->usb_dev, 0), HID_REQ_SET_REPORT,
                        USB_DIR_OUT | USB_TYPE_CLASS | USB_RECIP_INTERFACE,
                        (HID_OUTPUT_REPORT + 1) << 8 | 0x05,
                        intf->cur_altsetting->desc.bInterfaceNumber,
                        report, 2, USB_CTRL_SET_TIMEOUT);
        kfree(report);
    }
    schedule_delayed_work(&dev->rf_wake_work, msecs_to_jiffies(3500));
}

/* -EPROTO: reset the endpoint's data toggle from process context and resubmit. */
static void blackctl_intr_recover_work(struct work_struct *work)
{
    struct blackctl_device *dev = container_of(work, struct blackctl_device, intr_recover_work);

    if (atomic_inc_return(&dev->eproto_count) > 5)
        return;
    usb_clear_halt(dev->usb_dev, usb_rcvintpipe(dev->usb_dev, 4));
    usb_submit_urb(dev->intr_urb, GFP_KERNEL);
}

/*
 * On the V3 Pro and Xbox, usbhid does not poll endpoint 0x84, so a private URB
 * does. Vendor frames go to the cache; everything else (the volume dial and
 * media keys) is handed to the HID input stack exactly as usbhid would.
 */
static void blackctl_intr_callback(struct urb *urb)
{
    struct blackctl_device *dev = urb->context;
    u8 *data = urb->transfer_buffer;
    int len = urb->actual_length;

    switch (urb->status) {
    case 0:
        atomic_set(&dev->eproto_count, 0);
        if (len == REPORT_LEN && data[0] == 0x02) {
            blackctl_cache(dev, data);
        } else if (len > 0) {
            /* Xbox two-byte report 5: wireless link changed, refresh battery. */
            if (dev->model->family == XBOX && len == 2 && data[0] == 0x05)
                mod_delayed_work(system_percpu_wq, &dev->battery_work, msecs_to_jiffies(500));
            hid_input_report(dev->hdev, HID_INPUT_REPORT, data, len, 1);
        }
        usb_submit_urb(urb, GFP_ATOMIC);
        break;
    case -EPROTO:
        schedule_work(&dev->intr_recover_work);
        break;
    case -ENOENT:
    case -ESHUTDOWN:
    case -ENODEV:
        break;
    default:
        usb_submit_urb(urb, GFP_ATOMIC);
    }
}

/* Poison the URB first so neither its callback nor recovery can resubmit it,
 * then cancel the work it may have queued. */
static void blackctl_stop(struct blackctl_device *dev)
{
    usb_poison_urb(dev->intr_urb);
    cancel_delayed_work_sync(&dev->rf_wake_work);
    cancel_delayed_work_sync(&dev->battery_work);
    cancel_work_sync(&dev->intr_recover_work);
    cancel_work_sync(&dev->full_power_work);
    usb_free_urb(dev->intr_urb);
    if (dev->intr_buf)
        usb_free_coherent(dev->usb_dev, REPORT_LEN, dev->intr_buf, dev->intr_dma);
}

static int blackctl_probe(struct hid_device *hdev, const struct hid_device_id *id)
{
    struct usb_interface *intf = to_usb_interface(hdev->dev.parent);
    struct blackctl_device *dev;
    bool private_urb;
    int i, ret;

    dev = kzalloc(sizeof(*dev), GFP_KERNEL);
    if (!dev)
        return -ENOMEM;

    dev->hdev = hdev;
    dev->usb_dev = interface_to_usbdev(intf);
    for (i = 0; i < ARRAY_SIZE(models); i++)
        if (models[i].pid == id->product)
            dev->model = &models[i];
    mutex_init(&dev->lock);
    init_completion(&dev->response);
    dev->battery = -1;
    dev->charging = dev->sidetone = dev->thx = dev->ull = dev->game_chat = -1;
    dev->anc_mode = dev->anc_level = dev->power_save = -1;
    dev->eq_slot = dev->fn_button = dev->audio_prompts = -1;
    INIT_WORK(&dev->intr_recover_work, blackctl_intr_recover_work);
    INIT_WORK(&dev->full_power_work, blackctl_full_power_work);
    INIT_DELAYED_WORK(&dev->battery_work, blackctl_battery_work);
    INIT_DELAYED_WORK(&dev->rf_wake_work, blackctl_rf_wake_work);
    hid_set_drvdata(hdev, dev);

    /* Submit the endpoint 0x84 URB before hid_hw_start: the dongle starts
     * sending about 3 ms after usbhid's SET_IDLE. The plain V3 is left to
     * usbhid, whose reports reach blackctl_raw_event. */
    private_urb = dev->model->family != V3;
    if (private_urb) {
        dev->intr_buf = usb_alloc_coherent(dev->usb_dev, REPORT_LEN, GFP_KERNEL, &dev->intr_dma);
        dev->intr_urb = usb_alloc_urb(0, GFP_KERNEL);
        if (!dev->intr_buf || !dev->intr_urb) {
            ret = -ENOMEM;
            goto err_free;
        }
        usb_fill_int_urb(dev->intr_urb, dev->usb_dev, usb_rcvintpipe(dev->usb_dev, 4),
                         dev->intr_buf, REPORT_LEN, blackctl_intr_callback, dev, 1);
        dev->intr_urb->transfer_dma = dev->intr_dma;
        dev->intr_urb->transfer_flags |= URB_NO_TRANSFER_DMA_MAP;
        usb_submit_urb(dev->intr_urb, GFP_KERNEL);
    }

    ret = hid_parse(hdev);
    if (!ret)
        ret = hid_hw_start(hdev, HID_CONNECT_DEFAULT);
    if (ret) {
        hid_err(hdev, "HID start failed: %d\n", ret);
        goto err_free;
    }

    if (private_urb) {
        dev->opened = !hid_hw_open(hdev);
        /* hid_hw_start's usb_set_interface killed the pre-submitted URB. */
        usb_kill_urb(dev->intr_urb);
        usb_submit_urb(dev->intr_urb, GFP_KERNEL);
        schedule_delayed_work(&dev->rf_wake_work, msecs_to_jiffies(750));
    }

    if (intf->cur_altsetting->desc.bInterfaceProtocol == 0) {
        ret = sysfs_create_group(&hdev->dev.kobj, &blackctl_group);
        dev->attrs_created = !ret;
    }

    switch (dev->model->family) {
    case XBOX:
        mod_delayed_work(system_percpu_wq, &dev->battery_work, msecs_to_jiffies(1000));
        schedule_work(&dev->full_power_work);
        break;
    case V3PRO:
        blackctl_handshake(dev);
        blackctl_query_battery(dev);
        break;
    case V3:
        break;  /* its battery query drops the RF link; wait for pushes */
    }
    usb_disable_autosuspend(dev->usb_dev);
    return 0;

err_free:
    blackctl_stop(dev);
    kfree(dev);
    return ret;
}

static void blackctl_remove(struct hid_device *hdev)
{
    struct blackctl_device *dev = hid_get_drvdata(hdev);

    if (dev->attrs_created)
        sysfs_remove_group(&hdev->dev.kobj, &blackctl_group);
    if (dev->opened)
        hid_hw_close(hdev);
    hid_hw_stop(hdev);
    blackctl_stop(dev);
    kfree(dev);
}

static int blackctl_raw_event(struct hid_device *hdev, struct hid_report *report, u8 *data, int size)
{
    struct blackctl_device *dev = hid_get_drvdata(hdev);

    if (size == REPORT_LEN && data[0] == 0x02)
        blackctl_cache(dev, data);
    return 0;
}

static const struct hid_device_id blackctl_devices[] = {
    { HID_USB_DEVICE(USB_VENDOR_ID_RAZER, 0x0a55) },
    { HID_USB_DEVICE(USB_VENDOR_ID_RAZER, 0x0577) },
    { HID_USB_DEVICE(USB_VENDOR_ID_RAZER, 0x0576) },
    { HID_USB_DEVICE(USB_VENDOR_ID_RAZER, 0x057a) },
    { HID_USB_DEVICE(USB_VENDOR_ID_RAZER, 0x0579) },
    { }
};
MODULE_DEVICE_TABLE(hid, blackctl_devices);

static struct hid_driver blackctl_driver = {
    .name = "blackctl",
    .id_table = blackctl_devices,
    .probe = blackctl_probe,
    .remove = blackctl_remove,
    .raw_event = blackctl_raw_event,
};
module_hid_driver(blackctl_driver);

MODULE_DESCRIPTION("Razer BlackShark V3 family control driver");
MODULE_LICENSE("GPL");
