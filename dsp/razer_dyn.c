// SPDX-License-Identifier: GPL-2.0-or-later
// From Lynapse 2.0 (github.com/Stealthee/Lynapse2).
/* razer_dyn: mono dynamics stage for razerctl's Synapse/THX-style enhancement chains.
 *
 * One plugin covers every level-dependent THX behaviour that was measured on Windows (2026-11-03):
 *   - input gain (THX "normalization" is a fixed makeup gain followed by a compressor/limiter)
 *   - downward expander below a threshold, with ratio, soft knee and a floor (max attenuation)
 *   - hard mute below a second threshold (THX voice gate / noise cancellation "mute" region)
 *   - soft-knee compressor / limiter above a threshold
 * Static curve (L = detected level in dBFS after input gain, sine RMS when RMS detection is used):
 *   g_exp  = max(floor, (ratio_e - 1) * (L - T_e))      for L < T_e  (soft knee of width K around T_e)
 *   g_comp = (1/ratio_c - 1) * (L - T_c)                for L > T_c  (soft knee of width K around T_c)
 *   muted                                               for L < T_mute
 * Gain smoothing is one-pole in dB (time constants; a 90 % settle takes 2.3x): the expander/mute gain OPENS with Attack
 * and closes with Release; the compressor gain REDUCES with Attack and recovers with Release. Mute = DEPTH (-100 dB),
 * output exactly 0 while fully closed.
 * All settings are plain LADSPA controls, so PipeWire filter-chain can change them live (pw-cli set-param ... Props).
 * "Neutral" settings (ratio 1, mute -200, gain 0) make the stage a pass-through.
 *
 * Build: gcc -O2 -shared -fPIC -o razer_dyn.so razer_dyn.c -lm
 */
#include <ladspa.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

enum {
    P_IN, P_OUT,
    P_GAIN, P_EXP_T, P_EXP_R, P_EXP_FLOOR, P_MUTE_T, P_COMP_T, P_COMP_R, P_KNEE,
    P_ATTACK, P_RELEASE, P_DETECT, P_RMS_MS,
    P_COUNT
};

typedef struct {
    LADSPA_Data *p[P_COUNT];
    double sr;
    double env;    /* mean square (RMS mode) or peak envelope, linear */
    double ge;     /* smoothed expander/mute gain in dB (0 = open, >= DEPTH) */
    double gc;     /* smoothed compressor gain in dB (<= 0) */
} Dyn;

static LADSPA_Handle instantiate(const LADSPA_Descriptor *d, unsigned long sr)
{
    (void)d;
    Dyn *s = calloc(1, sizeof(Dyn));
    if (s) s->sr = (double)sr;
    return s;
}

static void connect(LADSPA_Handle h, unsigned long port, LADSPA_Data *data) { ((Dyn *)h)->p[port] = data; }

static void activate(LADSPA_Handle h)
{
    Dyn *s = h;
    s->env = 0.0;
    s->ge = 0.0;
    s->gc = 0.0;
}

/* soft-knee "below threshold" excess: how far L is under T, smoothed over knee width K (returns <= 0) */
static double below(double L, double T, double K)
{
    double x = L - T;
    if (K > 0.0 && fabs(x) < K / 2.0) { double y = x - K / 2.0; return -(y * y) / (2.0 * K); }
    return x < 0.0 ? x : 0.0;
}
/* soft-knee "above threshold" excess (returns >= 0) */
static double above(double L, double T, double K)
{
    double x = L - T;
    if (K > 0.0 && fabs(x) < K / 2.0) { double y = x + K / 2.0; return (y * y) / (2.0 * K); }
    return x > 0.0 ? x : 0.0;
}

/* deepest expander/mute attenuation; smoothing runs in dB, so opening from here takes ~5 attack time constants */
#define DEPTH (-100.0)

static double coef(double ms, double sr) { return ms <= 0.0 ? 0.0 : exp(-1.0 / (ms * 0.001 * sr)); }

static void run(LADSPA_Handle h, unsigned long n)
{
    Dyn *s = h;
    const LADSPA_Data *in = s->p[P_IN];
    LADSPA_Data *out = s->p[P_OUT];
    const double gain = pow(10.0, *s->p[P_GAIN] / 20.0);
    const double te = *s->p[P_EXP_T], re = *s->p[P_EXP_R] < 1.0 ? 1.0 : *s->p[P_EXP_R];
    const double floor_db = *s->p[P_EXP_FLOOR], tm = *s->p[P_MUTE_T];
    const double tc = *s->p[P_COMP_T], rc = *s->p[P_COMP_R] < 1.0 ? 1.0 : *s->p[P_COMP_R];
    const double knee = *s->p[P_KNEE] < 0.0 ? 0.0 : *s->p[P_KNEE];
    const double ca = coef(*s->p[P_ATTACK], s->sr), cr = coef(*s->p[P_RELEASE], s->sr);
    const int peak = *s->p[P_DETECT] >= 0.5f;
    const double cd = coef(*s->p[P_RMS_MS] > 0.1 ? *s->p[P_RMS_MS] : 0.1, s->sr);
    const int exp_on = re > 1.0001, comp_on = rc > 1.0001, mute_on = tm > -199.0;
    double env = s->env, ge = s->ge, gc = s->gc;

    for (unsigned long i = 0; i < n; i++) {
        double x = in[i] * gain, te_db = 0.0, tc_db = 0.0;
        if (exp_on || comp_on || mute_on) {
            double L;
            if (peak) {
                double a = fabs(x);
                env = a > env ? a : env * cd;            /* instant attack, decay over the detector window */
                L = 20.0 * log10(env + 1e-20);
            } else {
                env = cd * env + (1.0 - cd) * x * x;
                L = 10.0 * log10(env + 1e-30);          /* RMS level; sine RMS == the sine "level" in the THX data */
            }
            if (exp_on) {
                double g_e = (re - 1.0) * below(L, te, knee);
                te_db = g_e < floor_db ? floor_db : g_e;
                if (te_db < DEPTH) te_db = DEPTH;
            }
            if (mute_on && L < tm) te_db = DEPTH;
            if (comp_on) tc_db = (1.0 / rc - 1.0) * above(L, tc, knee);
        }
        ge = te_db > ge ? te_db + ca * (ge - te_db) : te_db + cr * (ge - te_db);       /* open = attack */
        gc = tc_db < gc ? tc_db + ca * (gc - tc_db) : tc_db + cr * (gc - tc_db);         /* reduce = attack */
        out[i] = (LADSPA_Data)(ge <= DEPTH + 0.01 ? 0.0 : x * pow(10.0, (ge + gc) / 20.0));
    }
    s->env = env < 1e-30 ? 0.0 : env;
    s->ge = ge;
    s->gc = gc;
}

static void cleanup(LADSPA_Handle h) { free(h); }

static const char *names[P_COUNT] = {
    "Input", "Output",
    "Input gain (dB)", "Expander threshold (dB)", "Expander ratio", "Expander floor (dB)", "Mute threshold (dB)",
    "Compressor threshold (dB)", "Compressor ratio", "Knee (dB)",
    "Attack (ms)", "Release (ms)", "Peak detect", "Detector window (ms)",
};
static const LADSPA_PortDescriptor pd[P_COUNT] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    [P_GAIN ... P_RMS_MS] = LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
};
#define HB(lo, hi, def) { LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | (def), (lo), (hi) }
static const LADSPA_PortRangeHint hints[P_COUNT] = {
    { 0, 0, 0 }, { 0, 0, 0 },
    HB(-60, 60, LADSPA_HINT_DEFAULT_0),          /* input gain */
    HB(-120, 0, LADSPA_HINT_DEFAULT_MINIMUM),     /* expander threshold */
    HB(1, 20, LADSPA_HINT_DEFAULT_1),             /* expander ratio (1 = off) */
    HB(-200, 0, LADSPA_HINT_DEFAULT_MINIMUM),     /* expander floor */
    HB(-200, 0, LADSPA_HINT_DEFAULT_MINIMUM),     /* mute threshold (-200 = off) */
    HB(-60, 0, LADSPA_HINT_DEFAULT_0),            /* compressor threshold */
    HB(1, 100, LADSPA_HINT_DEFAULT_1),            /* compressor ratio (1 = off) */
    HB(0, 24, LADSPA_HINT_DEFAULT_0),             /* knee */
    HB(0, 1000, LADSPA_HINT_DEFAULT_LOW),         /* attack */
    HB(0, 5000, LADSPA_HINT_DEFAULT_MIDDLE),      /* release */
    HB(0, 1, LADSPA_HINT_DEFAULT_0 | LADSPA_HINT_INTEGER),
    HB(0.1, 100, LADSPA_HINT_DEFAULT_LOW),        /* detector window */
};

static const LADSPA_Descriptor desc = {
    .UniqueID = 0x52d901,
    .Label = "razer_dyn",
    .Properties = LADSPA_PROPERTY_HARD_RT_CAPABLE,
    .Name = "razerctl dynamics (expander / gate / compressor)",
    .Maker = "razerctl",
    .Copyright = "GPL-2.0-or-later",
    .PortCount = P_COUNT,
    .PortDescriptors = pd,
    .PortNames = names,
    .PortRangeHints = hints,
    .instantiate = instantiate,
    .connect_port = connect,
    .activate = activate,
    .run = run,
    .cleanup = cleanup,
};

const LADSPA_Descriptor *ladspa_descriptor(unsigned long i) { return i == 0 ? &desc : NULL; }
