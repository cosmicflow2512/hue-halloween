"""Synthetische Halloween-Sets: Sound komplett generiert (eigene Kompositionen + Geräusche),
Lichtcues entstehen aus derselben Partitur -> Licht sitzt exakt auf Musik und Effekten.

Aufruf: python synth.py /shows   (erzeugt nur, was fehlt oder veraltet ist)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

SR = 32000
VERSION = 4


def mtof(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


# ---------------------------------------------------------------- Filter / Hilfen
def _sos(kind: str, f, order: int = 2):
    return butter(order, f, btype=kind, fs=SR, output="sos")


def lp(x, f, order=2):
    return sosfilt(_sos("lowpass", min(f, SR / 2 - 100), order), x)


def hp(x, f, order=2):
    return sosfilt(_sos("highpass", f, order), x)


def bp(x, lo, hi, order=2):
    return sosfilt(_sos("bandpass", [lo, min(hi, SR / 2 - 100)], order), x)


def tl(dur):
    return np.arange(int(dur * SR)) / SR


def exp_env(n, decay):
    return np.exp(-np.arange(n) / (decay * SR))


def adsr(n, a=0.01, d=0.1, s=0.7, r=0.2):
    a_n, d_n, r_n = int(a * SR), int(d * SR), int(r * SR)
    s_n = max(0, n - a_n - d_n - r_n)
    e = np.concatenate(
        [np.linspace(0, 1, a_n, endpoint=False), np.linspace(1, s, d_n, endpoint=False), np.full(s_n, s), np.linspace(s, 0, r_n)]
    )
    return np.pad(e, (0, max(0, n - len(e))))[:n]


def osc_sine(freq, n):
    f = np.broadcast_to(np.asarray(freq, dtype=np.float64), (n,))
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


def osc_saw(freq, n):
    f = np.broadcast_to(np.asarray(freq, dtype=np.float64), (n,))
    ph = np.cumsum(f) / SR
    return 2 * (ph - np.floor(ph + 0.5))


def smooth_noise(rng, n, points_per_sec=0.5, lo=0.0, hi=1.0):
    """Langsame Zufallskurve (z. B. Windstärke, Tonhöhenverlauf)."""
    k = max(2, int(n / SR * points_per_sec) + 2)
    pts = rng.uniform(lo, hi, k)
    xs = np.linspace(0, n, k)
    return np.interp(np.arange(n), xs, pts)


# ---------------------------------------------------------------- Instrumente
def musicbox(m, dur=2.2):
    f = mtof(m)
    n = int(dur * SR)
    t = np.arange(n) / SR
    s = (
        np.sin(2 * np.pi * f * t) * np.exp(-t / 0.9)
        + 0.35 * np.sin(2 * np.pi * f * 2 * t) * np.exp(-t / 0.35)
        + 0.18 * np.sin(2 * np.pi * f * 3.93 * t) * np.exp(-t / 0.12)
    )
    s[: int(0.002 * SR)] *= np.linspace(0, 1, int(0.002 * SR))
    return s * 0.5


def organ(m, dur):
    n = int(dur * SR)
    f = mtof(m)
    trem = 1 + 0.06 * np.sin(2 * np.pi * 5.8 * np.arange(n) / SR)
    s = np.zeros(n)
    for h, a in ((0.5, 0.5), (1, 1.0), (2, 0.6), (3, 0.4), (4, 0.3), (6, 0.15), (8, 0.12)):
        if f * h < SR / 2.2:
            s += a * osc_sine(f * h, n)
    return s * adsr(n, 0.05, 0.1, 0.9, 0.25) * trem * 0.12


def pad(m, dur, bright=1200):
    n = int(dur * SR)
    f = mtof(m)
    s = sum(osc_saw(f * 2 ** (c / 1200), n) for c in (-9, 0, 8))
    return lp(s, bright) * adsr(n, 1.2, 0.5, 0.8, 1.8) * 0.08


def choir(m, dur):
    n = int(dur * SR)
    f = mtof(m) * (1 + 0.006 * np.sin(2 * np.pi * 5.0 * np.arange(n) / SR))
    src = osc_saw(f, n) + 0.3 * np.random.default_rng(int(m)).normal(0, 0.3, n)
    s = bp(src, 650, 850) + 0.6 * bp(src, 1000, 1250) + 0.25 * bp(src, 2400, 2800)
    return s * adsr(n, 0.9, 0.3, 0.85, 1.2) * 0.25


def bass(m, dur):
    n = int(dur * SR)
    s = osc_sine(mtof(m), n) + 0.4 * osc_saw(mtof(m), n)
    return np.tanh(lp(s, 600) * 2) * adsr(n, 0.005, 0.08, 0.6, 0.05) * 0.35


def lead(m, dur):
    n = int(dur * SR)
    vib = 1 + 0.008 * np.sin(2 * np.pi * 5.5 * np.arange(n) / SR) * np.clip(np.arange(n) / (0.3 * SR), 0, 1)
    s = osc_saw(mtof(m) * vib, n) + 0.5 * osc_saw(mtof(m) * 1.005 * vib, n)
    return lp(s, 2200) * adsr(n, 0.02, 0.1, 0.7, 0.12) * 0.12


def kick():
    n = int(0.4 * SR)
    f = 45 + 90 * exp_env(n, 0.04)
    return np.tanh(osc_sine(f, n) * exp_env(n, 0.18) * 1.5) * 0.7


def snare(rng):
    n = int(0.22 * SR)
    return (bp(rng.normal(0, 1, n), 1500, 7000) * exp_env(n, 0.07) * 0.5 + osc_sine(185, n) * exp_env(n, 0.05) * 0.3)


def hat(rng):
    n = int(0.06 * SR)
    return hp(rng.normal(0, 1, n), 7000) * exp_env(n, 0.015) * 0.15


def bell(m, dur=7.0):
    n = int(dur * SR)
    f = mtof(m)
    t = np.arange(n) / SR
    s = np.zeros(n)
    for ratio, amp, dec in ((0.5, 0.6, 4.0), (1.0, 1.0, 3.0), (1.19, 0.6, 2.2), (1.5, 0.5, 1.8), (2.0, 0.45, 1.5),
                            (2.51, 0.3, 1.0), (2.66, 0.3, 0.9), (3.01, 0.25, 0.7), (4.1, 0.15, 0.4)):
        s += amp * np.sin(2 * np.pi * f * ratio * t) * np.exp(-t / dec)
    return s * 0.15


# ---------------------------------------------------------------- Geräusche
def thunder(rng, intensity=1.0, dur=6.0):
    n = int(dur * SR)
    crack_n = int(0.4 * SR)
    crack = hp(rng.normal(0, 1, crack_n), 300) * exp_env(crack_n, 0.06)
    brown = np.cumsum(rng.normal(0, 1, n))
    brown -= lp(brown, 5)  # Drift entfernen
    rumble = lp(brown, 180, 4)
    rumble /= np.max(np.abs(rumble)) + 1e-9
    env = (1 - exp_env(n, 0.08)) * exp_env(n, dur * 0.3)
    env *= 0.7 + 0.3 * smooth_noise(rng, n, 4)
    s = rumble * env * 0.9
    s[:crack_n] += crack * 0.8
    return s * intensity


def wind(rng, dur, strength=1.0):
    n = int(dur * SR)
    noise = rng.normal(0, 1, n)
    amp = smooth_noise(rng, n, 0.15, 0.2, 1.0)
    s = np.zeros(n)
    for lo, hi, mul in ((200, 400, 1.0), (400, 700, 0.7), (700, 1100, 0.45)):
        s += bp(noise, lo, hi, 2) * smooth_noise(rng, n, 0.25, 0.3, 1.0) * mul
    # Pfeifen: schmales Band mit wanderndem Zentrum (stückweise)
    whistle = np.zeros(n)
    seg = int(2 * SR)
    for i in range(0, n, seg):
        c = rng.uniform(500, 900)
        part = bp(noise[i : i + seg + SR], c * 0.97, c * 1.03, 2)[: min(seg, n - i)]
        w = np.hanning(len(part) * 2)[: len(part)] if i == 0 else np.ones(len(part))
        whistle[i : i + len(part)] = part * w
    s = (s + whistle * 0.8) * amp * strength * 0.5
    return s, amp


def rain(rng, dur, level=1.0):
    n = int(dur * SR)
    s = hp(rng.normal(0, 1, n), 2500) * 0.06
    drops = np.zeros(n)
    idx = rng.integers(0, n, int(dur * 40))
    drops[idx] = rng.uniform(0.2, 1.0, len(idx))
    s += bp(drops, 2000, 6000) * 0.5
    return s * level


def ghost(rng, dur=5.0):
    n = int(dur * SR)
    pitch = smooth_noise(rng, n, 1.2, 330, 720)
    pitch *= 1 + 0.02 * np.sin(2 * np.pi * 5.2 * np.arange(n) / SR)
    s = osc_sine(pitch, n) + 0.3 * osc_sine(pitch * 2, n) + 0.1 * osc_sine(pitch * 3, n)
    env = np.sin(np.linspace(0, np.pi, n)) ** 1.5
    return s * env * 0.18


def wolf(rng, dur=4.0):
    n = int(dur * SR)
    k = np.linspace(0, 1, n)
    pitch = np.interp(k, [0, 0.2, 0.7, 1], [380, 680, 620, 420]) * (1 + 0.01 * np.sin(2 * np.pi * 5 * k * dur))
    s = osc_sine(pitch, n) + 0.4 * osc_sine(pitch * 2, n) + 0.15 * osc_sine(pitch * 3, n)
    s += 0.05 * bp(rng.normal(0, 1, n), 500, 3000)
    env = np.interp(k, [0, 0.1, 0.8, 1], [0, 1, 0.8, 0])
    return s * env * 0.15


def creak(rng, dur=1.6):
    n = int(dur * SR)
    rate = np.interp(np.linspace(0, 1, n), [0, 0.5, 1], [25, 70, 35])
    ph = np.cumsum(rate) / SR
    pulses = np.zeros(n)
    pulses[np.nonzero(np.diff(np.floor(ph)) > 0)[0]] = 1.0
    pulses *= rng.uniform(0.5, 1.0, n)
    s = bp(pulses, 700, 1100, 4) * 6 + bp(pulses, 1800, 2300, 2) * 3
    return s * np.sin(np.linspace(0, np.pi, n)) * 0.35


def door_slam(rng):
    n = int(1.2 * SR)
    thump = osc_sine(60 + 60 * exp_env(n, 0.03), n) * exp_env(n, 0.15)
    rattle = bp(rng.normal(0, 1, n), 300, 2500) * exp_env(n, 0.08) * 0.4
    return (thump + rattle) * 0.8


def crow(rng):
    out = []
    for _ in range(rng.integers(2, 4)):
        n = int(0.32 * SR)
        k = np.linspace(0, 1, n)
        pitch = 900 - 250 * k
        s = osc_saw(pitch, n) * (0.6 + 0.4 * rng.normal(0, 1, n))
        s = bp(s, 800, 2600) * np.sin(np.pi * k) ** 0.7 * 0.25
        out.append(np.concatenate([s, np.zeros(int(rng.uniform(0.15, 0.35) * SR))]))
    return np.concatenate(out)


def owl():
    parts = []
    for d, p in ((0.35, 380), (0.15, 0), (0.7, 360)):
        n = int(d * SR)
        parts.append(osc_sine(p, n) * np.sin(np.linspace(0, np.pi, n)) * 0.25 if p else np.zeros(n))
    return np.concatenate(parts)


def bubble(rng):
    n = int(rng.uniform(0.04, 0.09) * SR)
    f0 = rng.uniform(180, 400)
    f = f0 * (1 + 2.5 * np.linspace(0, 1, n) ** 2)
    return osc_sine(f, n) * np.sin(np.linspace(0, np.pi, n)) * 0.25


def heartbeat_thump(strength=1.0):
    n = int(0.25 * SR)
    f = 40 + 30 * exp_env(n, 0.03)
    return np.tanh(osc_sine(f, n) * exp_env(n, 0.07) * 2) * 0.6 * strength


def witch_laugh(rng, syllables=8):
    out = []
    p = 620.0
    for i in range(syllables):
        n = int(0.16 * SR)
        k = np.linspace(0, 1, n)
        pitch = p * (1 + 0.15 * (1 - k)) * (1 + 0.03 * np.sin(2 * np.pi * 30 * k))
        src = osc_saw(pitch, n) + 0.4 * rng.normal(0, 1, n)
        s = bp(src, 700, 1300) + 0.5 * bp(src, 2300, 3000)
        s *= np.sin(np.pi * k) ** 0.5
        out.append(np.concatenate([s * 0.2, np.zeros(int(0.06 * SR))]))
        p *= 0.95
    return np.concatenate(out)


# ---------------------------------------------------------------- Mix + Cues
class Mix:
    def __init__(self, dur, seed):
        self.n = int(dur * SR)
        self.dur = dur
        self.buf = np.zeros((2, self.n))
        self.dry = np.zeros((2, self.n))  # ohne Hall (Drums)
        self.cues: list[dict] = []
        self.rng = np.random.default_rng(seed)

    def add(self, sig, t, pan=0.0, gain=1.0, dry=False):
        i = int(t * SR)
        if i >= self.n or i < 0:
            return
        sig = sig[: self.n - i] * gain
        lg, rg = np.sqrt((1 - pan) / 2), np.sqrt((1 + pan) / 2)
        target = self.dry if dry else self.buf
        target[0, i : i + len(sig)] += sig * lg
        target[1, i : i + len(sig)] += sig * rg

    def cue(self, t, fx, **p):
        if 0 <= t <= self.dur:
            self.cues.append({"t": round(float(t), 3), "fx": fx, **p})

    def envelope(self, t, curve_full, rate=25, lo=0.3):
        step = SR // rate
        vals = curve_full[::step]
        vals = (vals - vals.min()) / (np.ptp(vals) + 1e-9)
        self.cue(t, "envelope", dur=round(len(vals) / rate, 2), rate=rate, min=lo, values=[round(float(v), 3) for v in vals])

    def trim(self, end):
        """Länge nachträglich festlegen (Puffer werden großzügig angelegt)."""
        n = int(end * SR)
        self.n, self.dur = n, round(end, 2)
        self.buf, self.dry = self.buf[:, :n], self.dry[:, :n]
        self.cues = [c for c in self.cues if c["t"] <= end]

    def render(self, wet=0.35, room=2.8):
        n_ir = int(room * SR)
        ir = []
        for _ in range(2):
            r = self.rng.normal(0, 1, n_ir) * exp_env(n_ir, room / 5)
            ir.append(lp(r, 6000) / np.sqrt(np.sum(r**2)))
        out = np.zeros_like(self.buf)
        for c in range(2):
            out[c] = self.buf[c] * (1 - wet) + fftconvolve(self.buf[c], ir[c])[: self.n] * wet * 3 + self.dry[c]
        # Pegelausgleich: leise Atmosphäre anheben, laute Stellen etwas bremsen (draußen wichtig)
        mono = (out[0] + out[1]) / 2
        level = np.sqrt(np.maximum(lp(mono**2, 1.5), 1e-10))
        level /= np.sqrt(np.mean(mono**2)) + 1e-9
        gain = np.clip(level ** -0.55, 0.35, 4.0)
        gain = lp(gain, 0.8)
        out *= gain
        out /= np.sqrt(np.mean(out**2)) + 1e-9
        out *= 10 ** (-17 / 20)
        out = np.tanh(out * 1.3) / 1.3  # weicher Limiter
        fade = int(2 * SR)
        out[:, -fade:] *= np.linspace(1, 0, fade)
        out /= max(1.0, np.max(np.abs(out)) / 0.9)
        return np.nan_to_num(out).astype(np.float32)


def pan_to_channels(pan):
    return "left" if pan < -0.3 else "right" if pan > 0.3 else "all"


def storm_event(mx, t, intensity=1.0, distance=0.5, pan=None):
    """Blitz bei t, Donner mit Verzögerung je nach Entfernung."""
    pan = float(mx.rng.uniform(-0.8, 0.8)) if pan is None else pan
    mx.cue(t, "lightning", dur=0.8, channels=pan_to_channels(pan), intensity=round(min(1.0, 0.5 + intensity * 0.5), 2))
    mx.add(thunder(mx.rng, intensity), t + distance, pan=pan * 0.6, gain=intensity)


# ---------------------------------------------------------------- Kompositionen
WALTZ = [  # (Midi, Schläge) – eigene Spieluhr-Melodie in a-Moll, 3/4
    (76, 1), (69, 1), (72, 1), (71, 2), (68, 1), (69, 1), (72, 1), (76, 1), (77, 2), (76, 1),
    (74, 1), (77, 1), (81, 1), (80, 2), (76, 1), (72, 1), (71, 1), (69, 1), (71, 3),
    (76, 1), (69, 1), (72, 1), (71, 2), (68, 1), (69, 1), (72, 1), (76, 1), (77, 2), (76, 1),
    (77, 1), (76, 1), (74, 1), (72, 2), (71, 1), (69, 1), (72, 1), (71, 1), (69, 3),
]
WALTZ_BASS = [57, 52, 57, 50, 50, 52, 57, 52, 57, 52, 57, 50, 53, 57, 52, 57]  # Grundton pro Takt


def play_waltz(mx, t0, bpm=84, reps=2, warm="#ffb36b"):
    beat = 60 / bpm
    t = t0
    note_i = 0
    for _ in range(reps):
        # Melodie
        tm = t
        for m, b in WALTZ:
            mx.add(musicbox(m), tm, pan=float(mx.rng.uniform(-0.3, 0.3)))
            mx.cue(tm, "sparkle", idx=note_i, color=warm, decay=0.35, intensity=0.9)
            note_i += 1
            tm += b * beat
        # Begleitung: Grundton + zwei Akkordtöne
        for bar, root in enumerate(WALTZ_BASS):
            tb = t + bar * 3 * beat
            third = root + (3 if root in (57, 50, 52) else 4)
            mx.add(musicbox(root - 12, 2.5), tb, gain=0.7)
            mx.add(musicbox(third, 1.2), tb + beat, gain=0.35)
            mx.add(musicbox(root + 7, 1.2), tb + 2 * beat, gain=0.35)
        t += len(WALTZ_BASS) * 3 * beat
    return t


TOCCATA = [  # J. S. Bach, Toccata d-Moll BWV 565 – Anfangsmotiv (gemeinfrei), (Midi, Dauer s)
    (81, 0.11), (79, 0.11), (81, 1.3), (None, 0.5), (79, 0.14), (77, 0.14), (76, 0.14), (74, 0.14), (73, 0.9), (74, 1.6), (None, 0.7),
]


def play_toccata(mx, t0):
    t = t0
    for octave in (0, -12, -24):
        start = t
        for m, d in TOCCATA:
            if m is not None:
                mx.add(organ(m + octave, d + 0.15), t, gain=1.2)
                mx.add(organ(m + octave - 12, d + 0.15), t, gain=0.6)
                if d > 0.5:
                    mx.cue(t, "hold", dur=round(d, 2), color="#c00018", intensity=0.85, fade=0.1)
                else:
                    mx.cue(t, "flash", color="#ff2040", decay=0.15, intensity=0.6)
            t += d
        mx.add(organ(38, t - start), start + 1.6, gain=0.8)  # Pedal-D
    # Vermindert aufgebauter Akkord -> d-Moll
    for i, m in enumerate((49, 52, 55, 58, 61, 64)):
        mx.add(organ(m, 3.2 - i * 0.18), t + i * 0.18, gain=0.9)
        mx.cue(t + i * 0.18, "sparkle", idx=i, color="#a000ff", decay=0.6)
    t += 3.4
    for m in (38, 50, 57, 62, 65, 69):
        mx.add(organ(m, 4.5), t, gain=0.9)
    mx.cue(t, "flash", color="#ffffff", hold=0.1, decay=0.6)
    mx.cue(t + 0.3, "hold", dur=4.0, color="#7a0010", intensity=0.9, fade=0.8)
    return t + 5.0


CHORALE = [  # eigener Choral in d-Moll: (Akkord, Farbe)
    ([50, 57, 62, 65], "#5a0018"), ([46, 58, 62, 65], "#3a0060"), ([43, 58, 62, 67], "#200070"), ([45, 57, 61, 64], "#600010"),
    ([50, 57, 62, 65], "#5a0018"), ([41, 57, 60, 65], "#30004a"), ([48, 55, 60, 64], "#102060"), ([45, 57, 61, 64], "#600010"),
    ([50, 57, 62, 69], "#5a0018"), ([43, 55, 62, 70], "#3a0060"), ([46, 58, 65, 70], "#200070"), ([45, 57, 64, 73], "#700010"),
    ([50, 57, 65, 74], "#4a0030"), ([46, 58, 65, 74], "#30004a"), ([45, 57, 61, 69], "#600010"), ([38, 57, 62, 65], "#200018"),
]


def play_chorale(mx, t0, bpm=62, reps=2):
    beat = 60 / bpm
    t = t0
    for rep in range(reps):
        for i, (chord, color) in enumerate(CHORALE):
            d = 2 * beat if i % 4 != 3 else 3 * beat
            for m in chord:
                mx.add(organ(m + (12 if rep and m > 60 else 0), d + 0.1), t, gain=0.8)
                mx.add(choir(m + 12, d + 0.4), t, gain=0.5 if rep else 0.3)
            mx.cue(t, "base", color=color, bri=0.9, fade=round(d * 0.4, 2))
            mx.cue(t, "flash", color="#ff6a90", decay=0.25, intensity=0.35)
            t += d
    return t


GROOVE_BASS = [40, 40, 41, 40, 43, 40, 41, 39]  # e-phrygisch, Achtel
LEAD = [(64, 1), (67, 1), (65, 1), (64, 1), (71, 2), (69, 1), (67, 1), (65, 1), (64, 1), (65, 1), (67, 1), (64, 4),
        (64, 1), (67, 1), (65, 1), (64, 1), (72, 2), (71, 1), (69, 1), (71, 1), (69, 1), (67, 1), (65, 1), (64, 4)]


def play_groove(mx, t0, bars, bpm=100, with_lead=False, drums=True):
    beat = 60 / bpm
    for bar in range(bars):
        tb = t0 + bar * 4 * beat
        for k, m in enumerate(GROOVE_BASS):
            mx.add(bass(m, beat * 0.45), tb + k * beat / 2, gain=0.9)
        if drums:
            for b in range(4):
                tt = tb + b * beat
                if b in (0, 2):
                    mx.add(kick(), tt, dry=True)
                    mx.cue(tt, "flash", color="#20ff50", hold=0.02, decay=0.12, intensity=0.55)
                else:
                    mx.add(snare(mx.rng), tt, gain=0.8)
                    mx.cue(tt, "sparkle", idx=bar * 2 + b, color="#9000ff", decay=0.15)
                for h in range(2):
                    mx.add(hat(mx.rng), tt + h * beat / 2, pan=0.4, dry=True)
            if bar % 4 == 3:
                mx.add(kick(), tb + 3.5 * beat, dry=True)
        if bar % 8 == 0:
            mx.cue(tb, "sweep", dur=round(2 * beat, 2), color="#ff7a00", dir="lr" if bar % 16 == 0 else "rl")
    if with_lead:
        t = t0
        reps = max(1, bars // 8)
        i = 0
        for _ in range(reps):
            for m, b in LEAD:
                mx.add(lead(m, b * beat * 0.95), t, pan=-0.2)
                mx.cue(t, "sparkle", idx=m - 60, color="#ff8000", decay=0.3, intensity=0.9)
                t += b * beat
                i += 1
    # Pad drunter
    mx.add(pad(52, bars * 4 * beat), t0, gain=0.6)
    mx.add(pad(59, bars * 4 * beat), t0, gain=0.4)
    return t0 + bars * 4 * beat


# ---------------------------------------------------------------- Sets
def set_geisterhaus():
    dur = 420.0
    mx = Mix(dur, 11)
    w, amp = wind(mx.rng, dur)
    mx.add(w, 0, gain=1.2)
    mx.add(pad(45, dur), 0, gain=0.7)
    mx.add(pad(52, dur), 0, gain=0.3)
    mx.envelope(0, amp, lo=0.45)
    mx.cue(0, "base", color="#00402a", bri=0.8, fade=6)
    # Spieluhr-Passagen
    for start in (40.0, 190.0, 320.0):
        mx.cue(start - 3, "base", color="#3a0070", bri=0.8, fade=3)
        end = play_waltz(mx, start)
        mx.cue(end + 1, "base", color="#00402a", bri=0.8, fade=5)
    # Geister
    for t in (18, 135, 170, 285, 390):
        g = ghost(mx.rng, 6.0)
        mx.add(g, t, pan=float(mx.rng.uniform(-0.7, 0.7)), gain=0.9)
        mx.cue(t, "drift", dur=6.0, color="#9fe8ff", period=4.0, intensity=0.8)
    for t in (28, 120, 245, 300):
        mx.add(creak(mx.rng), t, pan=float(mx.rng.uniform(-0.8, 0.8)))
        mx.cue(t, "flicker", dur=1.6, color="#ffcc80", intensity=0.5, speed=3)
    for t in (100, 230, 355):
        mx.add(owl(), t, pan=0.6, gain=0.8)
    for t in (60, 150, 260, 375):
        storm_event(mx, t, intensity=0.5, distance=1.5)
    # Herzschlag-Passage, wird schneller
    t, bpm = 140.0, 58.0
    mx.cue(t - 1, "base", color="#100000", bri=1.0, fade=1.5)
    while t < 165:
        b = 60 / bpm
        mx.add(heartbeat_thump(), t, dry=True)
        mx.add(heartbeat_thump(0.7), t + 0.28 * b, dry=True)
        mx.cue(t, "flash", color="#ff0010", hold=0.03, decay=0.15, intensity=0.9)
        mx.cue(t + 0.28 * b, "flash", color="#ff0010", hold=0.02, decay=0.12, intensity=0.6)
        t += b
        bpm = min(120, bpm * 1.04)
    mx.add(door_slam(mx.rng), 166)
    mx.cue(166, "flash", color="#ffffff", hold=0.05, decay=0.1)
    mx.cue(166.3, "blackout", dur=2.5)
    mx.cue(169, "base", color="#00402a", bri=0.8, fade=4)
    return mx, "Geisterhaus", "Wind, Spieluhr, Geister, knarrende Türen, Herzschlag"


def set_friedhof():
    mx = Mix(600.0, 22)
    w, amp = wind(mx.rng, 600.0, 0.7)
    mx.add(w, 0, gain=0.8)
    mx.cue(0, "base", color="#001040", bri=0.7, fade=4)
    # Mitternacht: 12 Glockenschläge
    for i in range(12):
        t = 6 + i * 3.2
        mx.add(bell(45), t, gain=1.2)
        mx.cue(t, "flash", color="#c8d8ff", hold=0.05, decay=1.2, intensity=0.9)
    t = play_toccata(mx, 48.0)
    mx.cue(t, "base", color="#200030", bri=0.8, fade=3)
    mx.add(crow(mx.rng), t + 2, pan=-0.6)
    mx.cue(t + 2, "strobe", dur=0.8, hz=6, color="#ffffff", intensity=0.3, idx=0)
    t = play_chorale(mx, t + 8, reps=2)
    # Zwischenspiel: Geister, Chor, Wolf, Krähen
    mx.cue(t, "base", color="#001040", bri=0.7, fade=4)
    mx.add(pad(38, 70), t, gain=0.8)
    mx.add(pad(45, 70), t, gain=0.5)
    for k, tg in enumerate((t + 4, t + 30, t + 52)):
        mx.add(ghost(mx.rng, 7), tg, gain=1.0)
        mx.add(choir(57 + 3 * (k % 2), 8), tg, gain=0.6)
        mx.cue(tg, "drift", dur=7, color="#b0ffd0", period=5, intensity=0.8, phase=k)
    for tc in (t + 15, t + 44):
        mx.add(crow(mx.rng), tc, pan=float(mx.rng.uniform(-0.8, 0.8)))
        mx.cue(tc, "strobe", dur=0.8, hz=6, color="#ffffff", intensity=0.3, idx=int(mx.rng.integers(0, 8)))
    mx.add(wolf(mx.rng), t + 22, pan=-0.7)
    mx.cue(t + 22, "pulse", dur=4, color="#c0d0ff", period=4, channels="left")
    storm_event(mx, t + 38, intensity=0.6, distance=1.8)
    t = play_chorale(mx, t + 66, reps=2)
    for i in range(3):
        tb = t + 2 + i * 4
        mx.add(bell(45), tb, gain=1.0)
        mx.cue(tb, "flash", color="#c8d8ff", hold=0.05, decay=1.2, intensity=0.8)
    end = t + 22
    mx.cue(end - 8, "base", color="#000000", bri=0, fade=6)
    mx.envelope(0, amp[: int(end * SR)], lo=0.6)
    mx.trim(end)
    return mx, "Mitternacht auf dem Friedhof", "12 Glockenschläge, Toccata-Orgel, Choral mit Chor, Geister, Krähen, Wolf"


def set_hexenkessel():
    mx = Mix(600.0, 33)
    mx.cue(0, "base", color="#004010", bri=0.7, fade=3)
    mx.cue(1, "flicker", dur=18, color="#30ff40", intensity=0.7, speed=1.5, fade=2)
    mx.add(witch_laugh(mx.rng), 10, pan=0.3)
    mx.cue(10, "strobe", dur=1.7, hz=9, color="#60ff00")

    def interlude(t, laugh_syl=10):
        mx.cue(t, "flicker", dur=22, color="#40ff20", intensity=0.8, speed=2, fade=1)
        mx.add(witch_laugh(mx.rng, laugh_syl), t + 4, pan=-0.3)
        mx.cue(t + 4, "strobe", dur=2.2, hz=9, color="#a000ff")
        mx.add(pad(40, 22), t, gain=0.8)
        for i in range(5):
            mx.add(crow(mx.rng), t + 10 + i * 2.2, pan=float(mx.rng.uniform(-0.8, 0.8)), gain=0.6)
        return t + 22

    t = play_groove(mx, 20.0, 16)
    t = interlude(t)
    t = play_groove(mx, t, 32, with_lead=True)
    t = interlude(t, 12)
    t = play_groove(mx, t, 16)
    t = play_groove(mx, t, 32, with_lead=True)
    mx.cue(t, "flicker", dur=14, color="#30ff40", intensity=0.6, speed=1, fade=3)
    mx.add(witch_laugh(mx.rng, 12), t + 2)
    mx.cue(t + 2, "strobe", dur=2.6, hz=10, color="#60ff00")
    end = t + 15
    for _ in range(int(end * 3)):
        tb = float(mx.rng.uniform(0, end))
        mx.add(bubble(mx.rng), tb, pan=float(mx.rng.uniform(-0.5, 0.5)), gain=0.6)
    mx.cue(end - 6, "base", color="#000000", bri=0, fade=5)
    mx.trim(end)
    return mx, "Hexenkessel", "Gruseliger Groove, blubbernder Kessel, Hexenlachen, Krähen"


def set_sturmnacht():
    dur = 480.0
    mx = Mix(dur, 44)
    w, amp = wind(mx.rng, dur, 1.3)
    mx.add(w, 0)
    r = rain(mx.rng, dur)
    mx.add(r, 0, gain=0.9)
    mx.add(pad(38, dur, 500), 0, gain=0.5)
    mx.envelope(0, amp, lo=0.35)
    mx.cue(0, "base", color="#0a1a50", bri=0.8, fade=8)
    t = 8.0
    while t < dur - 15:
        inten = float(mx.rng.uniform(0.5, 1.0))
        storm_event(mx, t, intensity=inten, distance=float(mx.rng.uniform(0.0, 2.5)))
        t += float(mx.rng.uniform(10, 32))
    for tw in (70, 260, 410):
        p = float(mx.rng.uniform(-0.8, 0.8))
        mx.add(wolf(mx.rng), tw, pan=p)
        mx.cue(tw, "pulse", dur=4, color="#a0b0ff", period=4, channels=pan_to_channels(p), intensity=0.6)
    for tc in (130, 330):
        mx.add(creak(mx.rng), tc)
        mx.add(door_slam(mx.rng), tc + 1.7)
        mx.cue(tc + 1.7, "flash", color="#ffffff", decay=0.1)
    for tg in (190, 370):
        mx.add(ghost(mx.rng, 6), tg)
        mx.cue(tg, "drift", dur=6, color="#9fe8ff", period=3)
    return mx, "Sturmnacht", "Gewitter, Regen, heulender Wind, Wölfe, schlagende Türen"


SETS = {
    "set1_geisterhaus": set_geisterhaus,
    "set2_friedhof": set_friedhof,
    "set3_hexenkessel": set_hexenkessel,
    "set4_sturmnacht": set_sturmnacht,
}


def encode_mp3(audio: np.ndarray, path: Path) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
         "-codec:a", "libmp3lame", "-b:a", "192k", str(path)],
        input=audio.T.tobytes(), check=False,
    )
    if proc.returncode:
        raise RuntimeError("ffmpeg-Encoding fehlgeschlagen")


def build(root: str, force: bool = False) -> None:
    base = Path(root)
    for sid, fn in SETS.items():
        d = base / sid
        meta = d / "show.json"
        if not force and meta.exists():
            try:
                if json.loads(meta.read_text(encoding="utf-8")).get("generator_version") == VERSION and (d / "audio.mp3").exists():
                    continue
            except Exception:  # noqa: BLE001
                pass
        d.mkdir(parents=True, exist_ok=True)
        mx, name, desc = fn()
        encode_mp3(mx.render(), d / "audio.mp3")
        show = {
            "name": name,
            "description": desc,
            "audio": "audio.mp3",
            "duration": mx.dur,
            "generator_version": VERSION,
            "cues": sorted(mx.cues, key=lambda c: c["t"]),
        }
        meta.write_text(json.dumps(show, separators=(",", ":")), encoding="utf-8")
        print(f"Set erzeugt: {sid} ({len(mx.cues)} Cues)", flush=True)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "/shows", force="--force" in sys.argv)
