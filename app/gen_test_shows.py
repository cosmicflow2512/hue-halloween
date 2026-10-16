"""Erzeugt zwei Test-Shows (synthetischer Sound, keine Fremd-Audiodateien nötig).

- kalibrierung: Piep alle 2 s + weißer Flash -> Offset einstellen
- gewitter_test: Regen-/Windrauschen, 6 Donner mit Blitzen, Grusel-Grundlicht
"""

from __future__ import annotations

import json
import math
import random
import struct
import sys
import wave
from pathlib import Path

SR = 22050


def write_wav(path: Path, samples: list[float]) -> None:
    peak = max(1e-9, max(abs(s) for s in samples))
    norm = 0.9 / peak
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(s * norm * 32767)) for s in samples))


def calibration(dirpath: Path) -> None:
    dur, every = 30.0, 2.0
    buf = [0.0] * int(dur * SR)
    times = [round(1.0 + i * every, 3) for i in range(int((dur - 1.0) / every))]
    for t in times:
        s0 = int(t * SR)
        for i in range(int(0.06 * SR)):
            env = 1.0 if i < 0.05 * SR else 0.0
            buf[s0 + i] += env * math.sin(2 * math.pi * 1000 * i / SR)
    write_wav(dirpath / "audio.wav", buf)
    show = {
        "name": "Kalibrierung (Piep + Flash alle 2 s)",
        "audio": "audio.wav",
        "duration": dur,
        "cues": [{"t": 0, "fx": "base", "color": "#4a00a0", "bri": 0.3}]
        + [{"t": t, "fx": "flash", "color": "#ffffff", "hold": 0.08, "decay": 0.05} for t in times],
    }
    (dirpath / "show.json").write_text(json.dumps(show, indent=2), encoding="utf-8")


def thunderstorm(dirpath: Path) -> None:
    rnd = random.Random(42)
    dur = 60.0
    n = int(dur * SR)
    buf = [0.0] * n

    # Hintergrund: gefiltertes Rauschen (Regen/Wind), langsam moduliert
    lp = 0.0
    for i in range(n):
        lp += 0.08 * (rnd.uniform(-1, 1) - lp)
        mod = 0.6 + 0.4 * math.sin(2 * math.pi * i / SR / 7.0)
        buf[i] = lp * 0.12 * mod

    strikes = [6.0, 14.5, 21.0, 31.5, 40.0, 51.0]
    for t in strikes:
        s0 = int(t * SR)
        # Krachen: kurzes breitbandiges Rauschen
        for i in range(int(0.35 * SR)):
            if s0 + i < n:
                buf[s0 + i] += rnd.uniform(-1, 1) * math.exp(-i / (0.07 * SR)) * 0.9
        # Grollen: Brown-Noise, tiefpassgefiltert, langes Abklingen
        brown, lp2 = 0.0, 0.0
        roll = int(4.0 * SR)
        for i in range(roll):
            if s0 + i >= n:
                break
            brown = max(-1.0, min(1.0, brown + rnd.uniform(-0.04, 0.04)))
            lp2 += 0.02 * (brown - lp2)
            env = (1 - math.exp(-i / (0.08 * SR))) * math.exp(-i / (1.3 * SR))
            buf[s0 + i] += lp2 * env * 2.5
    write_wav(dirpath / "audio.wav", buf)

    cues = [
        {"t": 0, "fx": "base", "color": "#4a00a0", "bri": 0.5, "fade": 3.0},
        {"t": 10, "fx": "base", "color": "#00a060", "bri": 0.35, "fade": 2.0},
        {"t": 24, "fx": "flicker", "dur": 6, "color": "#ff5a00", "bri": 0.7},
        {"t": 30.5, "fx": "blackout", "dur": 1.0},
        {"t": 35, "fx": "pulse", "dur": 4, "color": "#aa0000", "period": 1.2},
        {"t": 44, "fx": "sweep", "dur": 1.5, "color": "#6a00ff", "dir": "lr"},
        {"t": 46, "fx": "sweep", "dur": 1.5, "color": "#6a00ff", "dir": "rl"},
        {"t": 55, "fx": "base", "color": "#000000", "bri": 0, "fade": 4.0},
    ]
    for i, t in enumerate(strikes):
        side = ["all", "left", "right"][i % 3]
        cues.append({"t": t, "fx": "lightning", "dur": 0.7, "channels": side})
    show = {"name": "Gewitter (Test)", "audio": "audio.wav", "duration": dur, "cues": cues}
    (dirpath / "show.json").write_text(json.dumps(show, indent=2), encoding="utf-8")


def main(root: str) -> None:
    base = Path(root)
    for name, fn in (("kalibrierung", calibration), ("gewitter_test", thunderstorm)):
        d = base / name
        if (d / "show.json").exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        fn(d)
        print(f"Test-Show erzeugt: {d}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/shows")
