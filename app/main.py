"""hue-halloween: Licht-Shows über die Hue Entertainment API, Sound über Home Assistant (Music Assistant / Sonos)."""

from __future__ import annotations

import asyncio
import json
import random
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from hue_entertainment import EntertainmentSession, HueEntertainmentAPI, LightColorCommand, discover_bridges

import analyze
import synth
from effects import Channel, Renderer, cues_from_json

LOG = logging.getLogger("hue-halloween")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SHOWS_DIR = Path(os.environ.get("SHOWS_DIR", "/shows"))
PORT = int(os.environ.get("PORT", "8099"))
HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
def _detect_public_url() -> str:
    """Eigene LAN-IP ermitteln (bei --network host = Unraid-IP), falls PUBLIC_URL nicht gesetzt ist."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))  # sendet nichts, wählt nur die Route
            return f"http://{s.getsockname()[0]}:{os.environ.get('PORT', '8099')}"
    except OSError:
        return ""


# URL, unter der Music Assistant / Sonos die Audiodatei abholt
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or _detect_public_url()).rstrip("/")
FPS = 50

SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULT_SETTINGS = {
    "hue_host": os.environ.get("HUE_HOST", ""),
    "app_key": "",
    "client_key": "",
    "area_id": "",
    "media_player": os.environ.get("MEDIA_PLAYER", ""),
    "offset_ms": int(os.environ.get("OFFSET_MS", "0")),
    "volume": None,  # 0..1 oder None = nicht ändern
    "play_mode": os.environ.get("PLAY_MODE", "announce"),  # announce | media
    "brightness": 1.0,
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    # ENV gewinnt, wenn gesetzt
    for key, env in (("hue_host", "HUE_HOST"), ("media_player", "MEDIA_PLAYER")):
        if os.environ.get(env):
            s[key] = os.environ[env]
    return s


def save_settings(s: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


class Player:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.session: EntertainmentSession | None = None
        self.task: asyncio.Task | None = None
        self.current: str | None = None
        self.show_time = 0.0
        self.last_error = ""
        self.http: aiohttp.ClientSession | None = None
        self.loop_task: asyncio.Task | None = None
        self.loop_info: dict | None = None
        self.busy = ""  # Hintergrundarbeit (Sets erzeugen, Analyse)

    # ---------- Hue ----------
    def _session(self) -> EntertainmentSession:
        s = self.settings
        if not (s["hue_host"] and s["app_key"] and s["client_key"]):
            raise RuntimeError("Hue nicht gekoppelt (erst 'Koppeln' ausführen)")
        if self.session is None or self.session.host != s["hue_host"]:
            self.session = EntertainmentSession(s["hue_host"], s["app_key"], s["client_key"], idle_timeout=5.0)
        return self.session

    async def pair(self, host: str | None) -> dict:
        if not host:
            found = await discover_bridges()
            if not found:
                raise RuntimeError("Keine Bridge gefunden – IP manuell eintragen")
            host = found[0].host
        api = HueEntertainmentAPI(host)
        try:
            creds = await api.pair(device_type="hue_halloween#unraid")
        finally:
            await api.close()
        self.settings.update(hue_host=host, app_key=creds["username"], client_key=creds["clientkey"])
        self.session = None
        save_settings(self.settings)
        return {"host": host}

    async def areas(self) -> list[dict]:
        areas = await self._session().get_entertainment_areas()
        return [
            {
                "id": a.id,
                "name": a.name,
                "channels": [{"id": c.channel_id, "name": c.name, "position": list(c.position)} for c in a.channels],
            }
            for a in areas
        ]

    # ---------- Home Assistant ----------
    async def _ha(self, domain: str, service: str, data: dict) -> None:
        if not (HA_URL and HA_TOKEN):
            raise RuntimeError("HA_URL/HA_TOKEN fehlen")
        assert self.http
        url = f"{HA_URL}/api/services/{domain}/{service}"
        async with self.http.post(url, json=data, headers={"Authorization": f"Bearer {HA_TOKEN}"}) as r:
            if r.status >= 300:
                raise RuntimeError(f"HA {domain}.{service}: HTTP {r.status} {await r.text()}")

    async def _start_audio(self, show_name: str, audio: str) -> None:
        mp = self.settings["media_player"]
        if not mp:
            raise RuntimeError("Kein media_player konfiguriert")
        if not PUBLIC_URL:
            raise RuntimeError("PUBLIC_URL fehlt")
        vol = self.settings.get("volume")
        url = f"{PUBLIC_URL}/media/{show_name}/{audio}"
        if self.settings.get("play_mode", "announce") == "announce":
            # Music Assistant Durchsage: pausiert laufende Wiedergabe und setzt sie danach fort
            data = {"entity_id": mp, "url": url, "use_pre_announce": False}
            if vol is not None:
                data["announce_volume"] = max(1, min(100, round(float(vol) * 100)))
            await self._ha("music_assistant", "play_announcement", data)
            return
        if vol is not None:
            await self._ha("media_player", "volume_set", {"entity_id": mp, "volume_level": float(vol)})
        await self._ha(
            "media_player",
            "play_media",
            {"entity_id": mp, "media_content_id": url, "media_content_type": "music"},
        )

    # ---------- Show ----------
    async def play(self, name: str, with_audio: bool = True) -> None:
        """Einzelne Show starten (beendet auch einen laufenden Endlos-Modus)."""
        await self.stop()
        await self._play_one(name, with_audio)

    async def play_loop(self, ids: list[str], shuffle: bool, gap: float = 4.0) -> None:
        await self.stop()
        self.loop_info = {"ids": ids, "shuffle": shuffle}

        async def runner():
            while True:
                order = list(ids)
                if shuffle:
                    random.shuffle(order)
                for sid in order:
                    try:
                        await self._play_one(sid, True)
                        if self.task:
                            await self.task
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # noqa: BLE001
                        LOG.exception("Endlos-Modus: %s fehlgeschlagen", sid)
                        self.last_error = f"{sid}: {err}"
                        await asyncio.sleep(10)
                    await asyncio.sleep(gap)

        self.loop_task = asyncio.create_task(runner())

    async def _play_one(self, name: str, with_audio: bool = True) -> None:
        await self._stop_show()
        show_dir = SHOWS_DIR / name
        data = json.loads((show_dir / "show.json").read_text(encoding="utf-8"))
        sess = self._session()
        area_id = self.settings["area_id"]
        areas = await sess.get_entertainment_areas()
        area = next((a for a in areas if a.id == area_id), None) or (areas[0] if areas else None)
        if area is None:
            raise RuntimeError("Kein Entertainment-Bereich auf der Bridge")
        channels = [Channel(c.channel_id, c.name, float(c.position[0])) for c in area.channels]
        renderer = Renderer(cues_from_json(data), channels)
        await sess.start(area.id)
        self.current = name
        self.last_error = ""
        self.task = asyncio.create_task(self._run(name, data, renderer, with_audio))

    async def _run(self, name: str, data: dict, renderer: Renderer, with_audio: bool) -> None:
        sess = self.session
        assert sess
        dur = float(data.get("duration", 60))
        frame = 1 / FPS
        try:
            # Grundlicht bei t=0 halten, bis der Audio-Aufruf durch ist
            self._send(renderer.render(0.0))
            if with_audio and data.get("audio"):
                await self._start_audio(name, data["audio"])
            t_ref = time.monotonic()
            nxt = t_ref
            while True:
                offset = self.settings.get("offset_ms", 0) / 1000
                t = time.monotonic() - t_ref - offset
                self.show_time = t
                if t > dur + 0.5:
                    break
                self._send(renderer.render(max(0.0, t)))
                nxt += frame
                await asyncio.sleep(max(0.0, nxt - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            LOG.exception("Show abgebrochen")
            self.last_error = str(err)
        finally:
            with_stop = asyncio.current_task() and asyncio.current_task().cancelling()
            await sess.stop()
            self.current = None
            media_mode = self.settings.get("play_mode", "announce") == "media"
            if with_stop and with_audio and media_mode and self.settings["media_player"] and HA_URL:
                try:
                    await self._ha("media_player", "media_stop", {"entity_id": self.settings["media_player"]})
                except Exception as err:  # noqa: BLE001
                    LOG.warning("media_stop fehlgeschlagen: %s", err)

    def _send(self, colors: dict) -> None:
        k = float(self.settings.get("brightness", 1.0)) * 65535
        self.session.send(
            [LightColorCommand(cid, int(r * k), int(g * k), int(b * k)) for cid, (r, g, b) in colors.items()]
        )

    async def stop(self) -> None:
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                pass
        self.loop_task = None
        self.loop_info = None
        await self._stop_show()

    async def _stop_show(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None


player = Player()
routes = web.RouteTableDef()


def ok(**kw) -> web.Response:
    return web.json_response({"ok": True, **kw})


def fail(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


@routes.get("/")
async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(Path(__file__).parent / "index.html")


@routes.get("/api/status")
async def status(_: web.Request) -> web.Response:
    s = {k: v for k, v in player.settings.items() if k not in ("app_key", "client_key")}
    s["paired"] = bool(player.settings["app_key"])
    return ok(
        settings=s,
        playing=player.current,
        loop=player.loop_info,
        busy=player.busy,
        show_time=round(player.show_time, 2),
        error=player.last_error,
        ha=bool(HA_URL and HA_TOKEN),
        public_url=PUBLIC_URL,
    )


@routes.get("/api/shows")
async def shows(_: web.Request) -> web.Response:
    out = []
    for d in sorted(SHOWS_DIR.iterdir()) if SHOWS_DIR.exists() else []:
        f = d / "show.json"
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({"id": d.name, "name": data.get("name", d.name), "duration": data.get("duration"),
                            "description": data.get("description", ""), "auto": bool(data.get("auto"))})
            except Exception as err:  # noqa: BLE001
                out.append({"id": d.name, "name": f"{d.name} (Fehler: {err})"})
    return ok(shows=out)


@routes.post("/api/pair")
async def pair(req: web.Request) -> web.Response:
    body = await req.json() if req.can_read_body else {}
    try:
        return ok(**await player.pair(body.get("host") or player.settings["hue_host"]))
    except Exception as err:  # noqa: BLE001
        return fail(str(err))


@routes.get("/api/areas")
async def areas(_: web.Request) -> web.Response:
    try:
        return ok(areas=await player.areas())
    except Exception as err:  # noqa: BLE001
        return fail(str(err))


@routes.post("/api/settings")
async def settings(req: web.Request) -> web.Response:
    body = await req.json()
    for key in ("area_id", "media_player", "offset_ms", "volume", "brightness", "hue_host", "play_mode"):
        if key in body:
            player.settings[key] = body[key]
    save_settings(player.settings)
    return ok()


@routes.post("/api/play/{name}")
async def play(req: web.Request) -> web.Response:
    name = req.match_info["name"]
    if "/" in name or ".." in name or not (SHOWS_DIR / name / "show.json").exists():
        return fail("Show nicht gefunden", 404)
    try:
        await player.play(name, with_audio=req.query.get("audio", "1") != "0")
        return ok(playing=name)
    except Exception as err:  # noqa: BLE001
        LOG.exception("Start fehlgeschlagen")
        return fail(str(err))


@routes.post("/api/loop")
async def loop(req: web.Request) -> web.Response:
    body = await req.json() if req.can_read_body else {}
    ids = body.get("ids") or [d.name for d in sorted(SHOWS_DIR.glob("set*")) if (d / "show.json").exists()]
    ids = [i for i in ids if "/" not in i and ".." not in i and (SHOWS_DIR / i / "show.json").exists()]
    if not ids:
        return fail("Keine Shows für den Endlos-Modus")
    try:
        await player.play_loop(ids, bool(body.get("shuffle", True)))
        return ok(ids=ids)
    except Exception as err:  # noqa: BLE001
        return fail(str(err))


@routes.post("/api/upload")
async def upload(req: web.Request) -> web.Response:
    reader = await req.multipart()
    tmp = SHOWS_DIR / ".upload"
    tmp.mkdir(exist_ok=True)
    saved = []
    async for part in reader:
        if not part.filename:
            continue
        name = Path(part.filename).name
        if Path(name).suffix.lower() not in analyze.AUDIO_EXT:
            return fail(f"Format nicht unterstützt: {name}")
        dst = tmp / name
        with dst.open("wb") as f:
            while chunk := await part.read_chunk(1 << 20):
                f.write(chunk)
        saved.append(dst)
    loop = asyncio.get_running_loop()
    ids = []
    player.busy = "Analysiere Upload…"
    try:
        for f in saved:
            ids.append(await loop.run_in_executor(None, lambda f=f: analyze.create_show(f, SHOWS_DIR, move=True)))
    except Exception as err:  # noqa: BLE001
        LOG.exception("Analyse fehlgeschlagen")
        return fail(f"Analyse fehlgeschlagen: {err}")
    finally:
        player.busy = ""
    return ok(ids=ids)


@routes.post("/api/rescan")
async def rescan(_: web.Request) -> web.Response:
    player.busy = "Suche neue Audiodateien…"
    try:
        ids = await asyncio.get_running_loop().run_in_executor(None, analyze.scan, SHOWS_DIR)
    except Exception as err:  # noqa: BLE001
        return fail(str(err))
    finally:
        player.busy = ""
    return ok(ids=ids)


@routes.post("/api/stop")
async def stop(_: web.Request) -> web.Response:
    await player.stop()
    return ok()


@routes.get("/media/{name}/{file}")
async def media(req: web.Request) -> web.StreamResponse:
    name, file = req.match_info["name"], req.match_info["file"]
    path = (SHOWS_DIR / name / file).resolve()
    if SHOWS_DIR.resolve() not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def _prepare() -> None:
    loop = asyncio.get_running_loop()
    try:
        player.busy = "Erzeuge Halloween-Sets (einmalig, ca. 1–2 min)…"
        await loop.run_in_executor(None, synth.build, str(SHOWS_DIR))
        player.busy = "Suche eigene Audiodateien…"
        await loop.run_in_executor(None, analyze.scan, SHOWS_DIR)
    except Exception as err:  # noqa: BLE001
        LOG.exception("Vorbereitung fehlgeschlagen")
        player.last_error = f"Vorbereitung: {err}"
    finally:
        player.busy = ""


async def on_startup(app: web.Application) -> None:
    player.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    app["prepare"] = asyncio.create_task(_prepare())


async def on_cleanup(_: web.Application) -> None:
    await player.stop()
    if player.session:
        await player.session.aclose()
    if player.http:
        await player.http.close()


def main() -> None:
    LOG.info("Audio-URL für Music Assistant: %s", PUBLIC_URL or "FEHLT")
    from gen_test_shows import main as gen

    SHOWS_DIR.mkdir(parents=True, exist_ok=True)
    gen(str(SHOWS_DIR))
    app = web.Application(client_max_size=1024**2)
    app.add_routes(routes)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, port=PORT)


if __name__ == "__main__":
    main()
