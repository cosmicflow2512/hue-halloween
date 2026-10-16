# hue-halloween

Halloween light and sound shows for Philips Hue, similar to OnSwitch, self-hosted.

Lights are driven through the **Hue Entertainment API** (DTLS stream, ~50 fps) using
[hue-entertainment](https://pypi.org/project/hue-entertainment/). Sound is played
through Home Assistant on a Music Assistant player (e.g. Sonos). A configurable
offset compensates the speaker buffering, so flashes land on the beat and on the thunder.

## Features

- **Four built-in Halloween sets**, generated on first start. Music and sound effects
  are synthesised by the container and the light cues come from the same score, so the
  lights follow every note, beat, bell and thunderclap.

  | Set | Length | Content |
  |---|---|---|
  | Geisterhaus | 7:00 | wind, music-box waltz, ghosts, creaking doors, owl, accelerating heartbeat, door slam |
  | Mitternacht auf dem Friedhof | 5:09 | 12 bell strikes, Bach's Toccata in D minor (public domain) on organ, chorale with choir, crows, wolf |
  | Hexenkessel | 5:09 | spooky groove (bass, drums, lead), bubbling cauldron, witch laughter, crows |
  | Sturmnacht | 8:00 | thunderstorm, rain, howling wind, wolves, slamming doors |

- **Endless "Halloween evening" mode**: plays the sets (optionally plus your own shows)
  in a loop, shuffled if wanted, until stopped.
- **Automatic light shows for your own audio** (MP3, M4A, FLAC, WAV, OGG): loudness
  envelope, bass hits → coloured pulses, high transients → sparkles running across the
  lamps, hard hits → lightning, quiet passages → candle flicker. Upload in the UI or drop
  files into the shows folder and press "neu scannen".
- **Calibration show** (beep + flash every 2 s) with a live offset slider.
- Left/right effects use the lamp positions from the Hue app's entertainment area setup.

## Requirements

- Hue bridge V2 or Pro with an **entertainment area**. Only colour-capable lights can be
  added, up to 10 per area, and only one area per bridge can stream at a time
  ([Hue FAQ](https://www.philips-hue.com/en-us/support/topic/entertainment/100085)).
- Home Assistant with the **Music Assistant** integration and a long-lived access token.
- Docker host network (mDNS discovery of the bridge; Music Assistant fetches the audio
  from this container over HTTP).

## Install (Unraid)

Add the template URL under *Docker → Template Repositories* or copy `hue-halloween.xml`
to `/boot/config/plugins/dockerMan/templates-user/`:

```
https://raw.githubusercontent.com/cosmicflow2512/hue-halloween/main/hue-halloween.xml
```

Or plain Docker:

```bash
docker run -d --name hue-halloween --restart unless-stopped --network host \
  -e HA_URL=http://192.168.0.10:8123 -e HA_TOKEN=xxx \
  -e MEDIA_PLAYER=media_player.wohnzimmer -e TZ=Europe/Berlin \
  -v /mnt/cache/appdata/hue-halloween/config:/config \
  -v /mnt/cache/appdata/hue-halloween/shows:/shows \
  ghcr.io/cosmicflow2512/hue-halloween:latest
```

## First start

1. Open `http://<server>:8099`. The status line shows the progress while the sets are generated.
2. Press the link button on the bridge, then click **Koppeln** within 30 s.
3. Pick the entertainment area and the media player.
4. Start **Kalibrierung** and move the offset slider until beep and flash coincide.
5. Start a set, or **Abend starten** for the endless mode.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `HA_URL` | – | Home Assistant base URL |
| `HA_TOKEN` | – | long-lived access token |
| `MEDIA_PLAYER` | – | Music Assistant `media_player` entity |
| `PLAY_MODE` | `announce` | `announce` = `music_assistant.play_announcement` (pauses and resumes current playback), `media` = `media_player.play_media` |
| `HUE_HOST` | auto | bridge IP, empty = mDNS discovery |
| `PUBLIC_URL` | auto | URL Music Assistant uses to fetch the audio, empty = detected server IP + port |
| `PORT` | `8099` | WebUI / audio server port |

Offset, brightness, volume, area and mode are stored in `/config/settings.json` via the UI.

## Show format

A show is a folder in `/shows` with `show.json` and an audio file.

```json
{
  "name": "Friedhof",
  "audio": "sound.mp3",
  "duration": 120,
  "cues": [
    {"t": 0,    "fx": "base",      "color": "#4a00a0", "bri": 0.5, "fade": 3},
    {"t": 12.4, "fx": "lightning", "dur": 0.7, "channels": "left"},
    {"t": 20,   "fx": "flicker",   "dur": 10, "color": "#ff5a00"},
    {"t": 31,   "fx": "sparkle",   "idx": 2, "color": "#ff8000"}
  ]
}
```

| fx | Parameters | Effect |
|---|---|---|
| `base` | `color`, `bri`, `fade` | background light, fades from the previous value |
| `envelope` | `values` (0..1), `rate`, `min`, `dur` | modulates the background brightness |
| `lightning` | `dur`, `strikes`, `intensity` | multi-strike lightning, slightly staggered per lamp |
| `flash` / `sparkle` | `color`, `hold`, `decay`, `intensity` | short flash with exponential decay |
| `flicker` | `dur`, `color`, `speed`, `fade` | candle / fire flicker |
| `pulse` | `dur`, `color`, `period`, `min` | breathing |
| `sweep` | `dur`, `color`, `dir` (`lr`/`rl`), `width` | light wave across the lamps |
| `drift` | `dur`, `color`, `period` | soft spot wandering back and forth (ghost) |
| `strobe` | `dur`, `hz`, `color` | strobe |
| `hold` | `dur`, `color`, `fade` | hold a colour |
| `blackout` | `dur` | all off |

Lamp selection for all effects except `base`/`envelope`: `"channels": "all" | "left" | "right" | [channel ids]`
or `"idx": n` (n-th lamp from the left, modulo lamp count).

## HTTP API

| Method | Path | |
|---|---|---|
| `POST` | `/api/play/<show>` | start a show (`?audio=0` = lights only) |
| `POST` | `/api/loop` | endless mode, body `{"ids": [...], "shuffle": true}`, empty = all sets |
| `POST` | `/api/stop` | stop |
| `GET` | `/api/status` | state |
| `GET` | `/api/shows` | list of shows |
| `POST` | `/api/upload` | multipart audio upload → automatic show |
| `POST` | `/api/rescan` | turn new audio files in `/shows` into shows |

Home Assistant example (`configuration.yaml`):

```yaml
rest_command:
  halloween_start:
    url: "http://192.168.0.20:8099/api/loop"
    method: post
    content_type: "application/json"
    payload: '{"shuffle": true}'
  halloween_stop:
    url: "http://192.168.0.20:8099/api/stop"
    method: post
```

## Notes

- While a show runs the entertainment area is streaming. Other control of those lamps
  is overridden until the show ends.
- The built-in music is original except the Toccata motif (J. S. Bach, public domain).
