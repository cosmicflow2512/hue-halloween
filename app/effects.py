"""Effekt-Engine: berechnet pro Frame die RGB-Werte (0..1) je Entertainment-Kanal.

Cue-Format (show.json -> "cues"): {"t": Sekunde, "fx": Name, ...Parameter}

Kanalauswahl (für alle Effekte außer base/envelope):
  "channels": "all" | "left" | "right" | [Kanal-IDs]
  "idx": n        -> n-te Lampe von links (modulo Anzahl), gut für Lauflichter
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

RGB = tuple[float, float, float]
BLACK: RGB = (0.0, 0.0, 0.0)
TAIL = {"lightning": 1.0, "flash": 1.0, "sparkle": 1.0}


def parse_color(value) -> RGB:
    """'#rrggbb' oder [r,g,b] (0..255) -> (r,g,b) 0..1."""
    if value is None:
        return (1.0, 1.0, 1.0)
    if isinstance(value, str):
        v = value.lstrip("#")
        return (int(v[0:2], 16) / 255, int(v[2:4], 16) / 255, int(v[4:6], 16) / 255)
    r, g, b = value
    return (r / 255, g / 255, b / 255)


def scale(c: RGB, k: float) -> RGB:
    return (c[0] * k, c[1] * k, c[2] * k)


def lerp(a: RGB, b: RGB, k: float) -> RGB:
    k = max(0.0, min(1.0, k))
    return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k)


def cmax(a: RGB, b: RGB) -> RGB:
    return (max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]))


@dataclass
class Channel:
    channel_id: int
    name: str
    x: float  # -1 (links) .. 1 (rechts), aus der Hue-App-Positionierung
    rank: int = 0  # Reihenfolge von links, wird vom Renderer gesetzt


@dataclass
class Cue:
    t: float
    fx: str
    params: dict = field(default_factory=dict)
    end: float = 0.0
    color: RGB = (1.0, 1.0, 1.0)

    @property
    def dur(self) -> float:
        return float(self.params.get("dur", 0.0))


class Renderer:
    """Zustandsbehafteter Renderer für eine Show (Cues sortiert nach t)."""

    def __init__(self, cues: list[Cue], channels: list[Channel]):
        self.cues = sorted(cues, key=lambda c: c.t)
        for ch_rank, ch in enumerate(sorted(channels, key=lambda c: (c.x, c.channel_id))):
            ch.rank = ch_rank
        self.channels = channels
        self.n = max(1, len(channels))
        for c in self.cues:
            c.end = c.t + c.dur + TAIL.get(c.fx, 0.0)
            default = "#dfe8ff" if c.fx in ("lightning", "flash") else "#ffffff"
            c.color = parse_color(c.params.get("color", default))
        self.base_cues = [c for c in self.cues if c.fx == "base"]
        self.env_cues = [c for c in self.cues if c.fx == "envelope"]
        self.fx_cues = [c for c in self.cues if c.fx not in ("base", "envelope")]
        self._fx_starts = [c.t for c in self.fx_cues]
        self._base_ptr = 0
        self._fx_ptr = 0
        self._active: list[tuple[int, Cue]] = []
        self._last_t = -1.0
        self.base_from = {ch.channel_id: BLACK for ch in channels}
        self.base_to = {ch.channel_id: BLACK for ch in channels}
        self.base_t0 = {ch.channel_id: 0.0 for ch in channels}
        self.base_fade = {ch.channel_id: 0.0 for ch in channels}
        self._strikes: dict[int, list[tuple[float, float, float]]] = {}
        xs = [c.x for c in channels] or [0.0]
        self._xmin, self._xmax = min(xs), max(xs)

    # ---------- Auswahl ----------
    def select(self, cue: Cue, ch: Channel) -> bool:
        p = cue.params
        if "idx" in p:
            return ch.rank == int(p["idx"]) % self.n
        sel = p.get("channels", "all")
        if sel == "all":
            return True
        if sel == "left":
            return ch.x <= 0
        if sel == "right":
            return ch.x >= 0
        if isinstance(sel, list):
            return ch.channel_id in sel
        return True

    # ---------- Grundlicht ----------
    def _base(self, ch: Channel, t: float) -> RGB:
        cid = ch.channel_id
        fade = self.base_fade[cid]
        k = 1.0 if fade <= 0 else (t - self.base_t0[cid]) / fade
        return lerp(self.base_from[cid], self.base_to[cid], k)

    def _advance(self, t: float) -> None:
        if t < self._last_t:  # Zurückspulen -> neu aufbauen
            self.__init__(self.cues, self.channels)  # type: ignore[misc]
        self._last_t = t
        while self._base_ptr < len(self.base_cues) and self.base_cues[self._base_ptr].t <= t:
            cue = self.base_cues[self._base_ptr]
            self._base_ptr += 1
            target = scale(cue.color, float(cue.params.get("bri", 1.0)))
            for ch in self.channels:
                if self.select(cue, ch):
                    cid = ch.channel_id
                    self.base_from[cid] = self._base(ch, cue.t)
                    self.base_to[cid] = target
                    self.base_t0[cid] = cue.t
                    self.base_fade[cid] = float(cue.params.get("fade", 0.0))
        while self._fx_ptr < len(self.fx_cues) and self.fx_cues[self._fx_ptr].t <= t:
            self._active.append((self._fx_ptr, self.fx_cues[self._fx_ptr]))
            self._fx_ptr += 1
        self._active = [(i, c) for i, c in self._active if c.end >= t]

    def _envelope(self, t: float) -> float:
        k = 1.0
        for cue in self.env_cues:
            if cue.t <= t <= cue.t + cue.dur:
                vals = cue.params["values"]
                pos = (t - cue.t) * float(cue.params.get("rate", 25))
                i = int(pos)
                if i + 1 < len(vals):
                    v = vals[i] + (vals[i + 1] - vals[i]) * (pos - i)
                else:
                    v = vals[-1] if vals else 1.0
                lo = float(cue.params.get("min", 0.2))
                k *= lo + (1 - lo) * v
        return k

    # ---------- Blitz ----------
    def _lightning_strikes(self, idx: int, cue: Cue) -> list[tuple[float, float, float]]:
        """Deterministische Einschläge: (Start relativ, Abklingzeit, Peak)."""
        if idx not in self._strikes:
            rnd = random.Random(int(cue.t * 1000) + idx)
            n = int(cue.params.get("strikes", rnd.randint(2, 4)))
            dur = max(cue.dur, 0.2)
            out, s = [], 0.0
            for i in range(n):
                peak = 1.0 if i == 0 else rnd.uniform(0.5, 1.0)
                out.append((s, rnd.uniform(0.04, 0.12), peak))
                s += rnd.uniform(0.06, dur / max(n, 1))
                if s >= dur:
                    break
            self._strikes[idx] = out
        return self._strikes[idx]

    def _fx(self, idx: int, cue: Cue, ch: Channel, t: float, base: RGB) -> RGB:
        rel = t - cue.t
        p = cue.params
        fx = cue.fx
        color = cue.color
        inten = float(p.get("intensity", p.get("bri", 1.0)))

        if fx == "lightning":
            jitter = (ch.channel_id * 0.017) % 0.05 if p.get("spread", True) else 0.0
            level = 0.0
            for start, decay, peak in self._lightning_strikes(idx, cue):
                d = rel - start - jitter
                if d >= 0:
                    level = max(level, peak * math.exp(-d / decay))
            return cmax(base, scale(color, level * inten))

        if fx in ("flash", "sparkle"):
            decay = float(p.get("decay", 0.08 if fx == "flash" else 0.25))
            hold = float(p.get("hold", 0.05 if fx == "flash" else 0.0))
            level = 1.0 if rel < hold else math.exp(-(rel - hold) / decay)
            return cmax(base, scale(color, level * inten))

        if fx == "flicker":
            ph = ch.channel_id * 1.7
            speed = float(p.get("speed", 1.0))
            n = (
                0.55
                + 0.25 * math.sin(rel * 7.1 * speed + ph)
                + 0.15 * math.sin(rel * 13.3 * speed + ph * 2.1)
                + 0.10 * math.sin(rel * 29.7 * speed + ph * 0.7)
            )
            fade = min(1.0, rel / float(p.get("fade", 0.01)), (cue.dur - rel) / float(p.get("fade", 0.01)))
            return lerp(base, scale(color, max(0.05, min(1.0, n)) * inten), fade)

        if fx == "pulse":
            period = float(p.get("period", 1.0))
            k = 0.5 - 0.5 * math.cos(2 * math.pi * rel / period)
            lo = float(p.get("min", 0.05))
            return lerp(base, scale(color, inten), lo + (1 - lo) * k)

        if fx == "sweep":
            span = (self._xmax - self._xmin) or 1.0
            pos = (ch.x - self._xmin) / span if self.n > 1 else 0.5
            if p.get("dir", "lr") == "rl":
                pos = 1 - pos
            front = rel / max(cue.dur, 0.1) * 1.4 - 0.2
            width = float(p.get("width", 0.25))
            level = max(0.0, 1 - abs(pos - front) / width)
            return cmax(base, scale(color, level * inten))

        if fx == "drift":
            # Geisterlicht: weicher Lichtfleck wandert hin und her, blendet ein/aus
            period = float(p.get("period", 6.0))
            pos = 0.5 + 0.5 * math.sin(2 * math.pi * rel / period + float(p.get("phase", 0.0)))
            mine = ch.rank / (self.n - 1) if self.n > 1 else 0.5
            level = math.exp(-((mine - pos) ** 2) / 0.08)
            env = min(1.0, rel / 1.5, max(0.0, (cue.dur - rel) / 1.5))
            return cmax(base, scale(color, level * env * inten))

        if fx == "blackout":
            return BLACK

        if fx == "strobe":
            hz = float(p.get("hz", 8.0))
            on = (rel * hz) % 1.0 < 0.3
            return scale(color, inten) if on else BLACK

        if fx == "hold":
            # Farbe für dur halten (überschreibt Grundlicht)
            fade = float(p.get("fade", 0.0))
            k = 1.0 if fade <= 0 else min(1.0, rel / fade, max(0.0, (cue.dur - rel) / fade))
            return lerp(base, scale(color, inten), k)

        return base

    def render(self, t: float) -> dict[int, RGB]:
        self._advance(t)
        env = self._envelope(t) if self.env_cues else 1.0
        out: dict[int, RGB] = {}
        for ch in self.channels:
            c = scale(self._base(ch, t), env)
            for idx, cue in self._active:
                if t <= cue.t + cue.dur + TAIL.get(cue.fx, 0.0) and self.select(cue, ch):
                    c = self._fx(idx, cue, ch, t, c)
            out[ch.channel_id] = (min(1.0, c[0]), min(1.0, c[1]), min(1.0, c[2]))
        return out


def cues_from_json(data: dict) -> list[Cue]:
    cues = []
    for raw in data.get("cues", []):
        raw = dict(raw)
        t = float(raw.pop("t"))
        fx = raw.pop("fx")
        cues.append(Cue(t=t, fx=fx, params=raw))
    return cues
