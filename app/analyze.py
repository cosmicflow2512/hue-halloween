"""Auto-Lichtshow für eigene Audiodateien (MP3/M4A/FLAC/WAV/OGG).

Analysiert Lautstärkeverlauf, Bass-Schläge, hohe Transienten und ruhige Passagen
und erzeugt daraus Cues im normalen show.json-Format.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

SR = 22050
HOP = 512
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"}

PALETTES = [  # (Grundlicht, Bass-Akzent, Höhen-Akzent)
    ("#3a0070", "#ff7a00", "#20ff50"),
    ("#00402a", "#a000ff", "#ff7a00"),
    ("#500010", "#ff2040", "#ffffff"),
    ("#0a1a50", "#9fe8ff", "#a000ff"),
    ("#402000", "#ff5a00", "#ffd000"),
]


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def _peaks(x: np.ndarray, fps: float, min_gap: float, k: float) -> list[int]:
    """Lokale Maxima über adaptivem Schwellwert (gleitender Median + k * Streuung)."""
    w = max(3, int(fps * 1.0))
    pad = np.pad(x, (w, w), mode="edge")
    strided = np.lib.stride_tricks.sliding_window_view(pad, 2 * w + 1)
    med = np.median(strided, axis=1)
    mad = np.median(np.abs(strided - med[:, None]), axis=1) + 1e-9
    thr = med + k * mad * 1.4826
    cand = np.nonzero((x > thr) & (x >= np.roll(x, 1)) & (x >= np.roll(x, -1)))[0]
    out, last = [], -10**9
    gap = int(min_gap * fps)
    for i in cand:
        if i - last >= gap:
            out.append(int(i))
            last = i
    return out


def analyze(path: Path, palette_offset: int = 0) -> dict:
    y = decode(path)
    if len(y) < SR:
        raise ValueError("Datei zu kurz")
    dur = len(y) / SR
    fps = SR / HOP
    n_fft = 2048
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(y, (n_fft // 2, n_fft // 2)), n_fft)[::HOP]
    spec = np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)
    logspec = np.log1p(spec * 10)
    flux = np.maximum(0, np.diff(logspec, axis=0, prepend=logspec[:1]))
    bass_band = (freqs >= 30) & (freqs < 160)
    high_band = (freqs >= 2000) & (freqs < 9000)
    bass_on = flux[:, bass_band].sum(axis=1)
    high_on = flux[:, high_band].sum(axis=1)
    all_on = flux.sum(axis=1)
    rms = np.sqrt(np.mean(frames**2, axis=1))

    def norm(v):
        lo, hi = np.percentile(v, 5), np.percentile(v, 98)
        return np.clip((v - lo) / (hi - lo + 1e-9), 0, 1)

    loud = norm(np.convolve(rms, np.ones(int(fps * 0.25)) / int(fps * 0.25), mode="same"))
    cues: list[dict] = []

    # Abschnitte: Paletten-Wechsel bei großen Lautstärke-Sprüngen, spätestens alle 40 s
    slow = np.convolve(loud, np.ones(int(fps * 6)) / int(fps * 6), mode="same")
    sections = [0.0]
    last = 0.0
    for i in range(0, len(slow), int(fps)):
        t = i / fps
        if t - last >= 40 or (t - last >= 12 and abs(slow[i] - slow[int(last * fps)]) > 0.35):
            sections.append(t)
            last = t
    for k, t in enumerate(sections):
        base, _, _ = PALETTES[(k + palette_offset) % len(PALETTES)]
        cues.append({"t": round(t, 2), "fx": "base", "color": base, "bri": 0.85, "fade": 3.0})

    def palette_at(t):
        k = max(i for i, s in enumerate(sections) if s <= t)
        return PALETTES[(k + palette_offset) % len(PALETTES)]

    # Lautstärke-Hüllkurve -> Grundlicht atmet mit
    step = max(1, int(fps / 25))
    env = loud[::step]
    cues.append({"t": 0, "fx": "envelope", "dur": round(len(env) / 25, 2), "rate": 25, "min": 0.25,
                 "values": [round(float(v), 3) for v in env]})

    # Sehr starke Transienten -> Blitz
    big = set()
    strong = np.percentile(all_on, 99.7)
    for i in _peaks(all_on, fps, 2.0, 6.0):
        if all_on[i] >= strong and loud[i] > 0.4:
            big.add(i)
            cues.append({"t": round(i / fps, 3), "fx": "lightning", "dur": 0.6,
                         "channels": ["all", "left", "right"][len(big) % 3]})

    # Bass-Schläge -> Flash in Akzentfarbe 1
    for i in _peaks(bass_on, fps, 0.2, 4.5):
        if i in big or loud[i] < 0.15:
            continue
        _, c1, _ = palette_at(i / fps)
        cues.append({"t": round(i / fps, 3), "fx": "flash", "color": c1, "hold": 0.02, "decay": 0.12,
                     "intensity": round(0.35 + 0.6 * float(loud[i]), 2)})

    # Hohe Transienten -> Funken, Lampe für Lampe
    n = 0
    for i in _peaks(high_on, fps, 0.15, 4.0):
        if i in big or loud[i] < 0.1:
            continue
        _, _, c2 = palette_at(i / fps)
        cues.append({"t": round(i / fps, 3), "fx": "sparkle", "idx": n, "color": c2, "decay": 0.2,
                     "intensity": round(0.4 + 0.5 * float(loud[i]), 2)})
        n += 1

    # Ruhige Passagen (> 4 s) -> Kerzenflackern
    quiet = loud < 0.12
    i = 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) / fps > 4:
                cues.append({"t": round(i / fps, 2), "fx": "flicker", "dur": round((j - i) / fps, 2),
                             "color": "#ff6a10", "intensity": 0.5, "fade": 1.0})
            i = j
        i += 1

    cues.sort(key=lambda c: c["t"])
    return {"duration": round(dur, 2), "cues": cues}


def slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß_-]+", "_", Path(name).stem).strip("_")
    return s[:60] or "show"


def create_show(audio_src: Path, shows_root: Path, title: str | None = None, move: bool = False,
                in_place: bool = False) -> str:
    """Audio analysieren und show.json schreiben. Gibt die Show-ID (Ordnername) zurück.

    in_place=True: die Datei liegt schon in ihrem Show-Ordner und bleibt dort.
    """
    if in_place:
        d, target = audio_src.parent, audio_src
        sid = d.name
    else:
        sid = "auto_" + slug(audio_src.name)
        d = shows_root / sid
        d.mkdir(parents=True, exist_ok=True)
        target = d / ("audio" + audio_src.suffix.lower())
        if audio_src.resolve() != target.resolve():
            (shutil.move if move else shutil.copyfile)(str(audio_src), str(target))
    result = analyze(target, palette_offset=sum(map(ord, sid)) % len(PALETTES))
    show = {"name": title or Path(audio_src.name).stem, "description": "Automatisch aus Audio erzeugt",
            "audio": target.name, "auto": True, **result}
    (d / "show.json").write_text(json.dumps(show, separators=(",", ":")), encoding="utf-8")
    return sid


def scan(shows_root: Path) -> list[str]:
    """Lose Audiodateien im Show-Ordner und Ordner ohne show.json automatisch umwandeln."""
    created = []
    for f in sorted(shows_root.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO_EXT:
            created.append(create_show(f, shows_root, move=True))
        elif f.is_dir() and not (f / "show.json").exists():
            audio = next((a for a in sorted(f.iterdir()) if a.suffix.lower() in AUDIO_EXT), None)
            if audio:
                created.append(create_show(audio, shows_root, title=f.name, in_place=True))
    return created
