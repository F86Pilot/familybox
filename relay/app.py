"""Familybox relay.

Sits between a travelling parent's iPhone and a Waveshare ESP32-S3 display
sitting on a five-year-old's bedside table. Everything the device receives
has already been converted into a format it can render without decoding.

See ../docs/PROTOCOL.md for the wire contract.
"""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import (
    Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse

import media
from store import Store

TOKEN = os.environ.get("FAMILYBOX_TOKEN", "")
DATA = Path(os.environ.get("FAMILYBOX_DATA", "./media"))
NTFY_TOPIC = os.environ.get("FAMILYBOX_NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("FAMILYBOX_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
BIG_ENDIAN = os.environ.get("RGB565_BIG_ENDIAN", "0") == "1"
MAX_AUDIO_SEC = int(os.environ.get("MAX_AUDIO_SEC", "60"))
RETAIN_DAYS = int(os.environ.get("FAMILYBOX_RETAIN_DAYS", "0"))  # 0 = keep forever

if not TOKEN or TOKEN == "changeme":
    raise SystemExit(
        "FAMILYBOX_TOKEN is unset or still the placeholder.\n"
        "Generate one with:  openssl rand -hex 32"
    )

STATIC = Path(__file__).parent / "static"

store = Store(DATA)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if RETAIN_DAYS:
        for msg_id in store.delete_older_than(RETAIN_DAYS):
            shutil.rmtree(DATA / str(msg_id), ignore_errors=True)
    yield


app = FastAPI(title="Familybox relay", version="1.0", lifespan=lifespan)


def require_token(authorization: str = Header(default="")) -> None:
    """Constant-time bearer check.

    An attacker who guesses this gets a live microphone in a child's
    bedroom, so it is worth not leaking timing on the comparison.
    """
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented, TOKEN):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


Auth = Depends(require_token)


def _save_upload(upload: UploadFile, suffix: str) -> Path:
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp)
    with tmp_path.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return tmp_path


# ---------------------------------------------------------------- inbound

@app.post("/api/v1/send", status_code=201, dependencies=[Auth])
async def send(
    photo: UploadFile | None = File(default=None),
    audio: UploadFile | None = File(default=None),
    sender: str = Form(default="dad"),
):
    """From the iOS share-sheet Shortcut."""
    if photo is None and audio is None:
        raise HTTPException(400, "need at least one of photo or audio")

    msg_id = store.create("inbound", sender=sender[:32])
    dest = store.msg_dir(msg_id)
    fields: dict[str, int] = {}
    staged: list[Path] = []

    try:
        if photo is not None:
            src = _save_upload(photo, Path(photo.filename or "in").suffix or ".bin")
            staged.append(src)
            fields["photo_bytes"] = media.photo_to_rgb565(
                src, dest / "photo.rgb565", big_endian=BIG_ENDIAN
            )
            media.photo_preview_jpeg(src, dest / "preview.jpg")

        if audio is not None:
            src = _save_upload(audio, Path(audio.filename or "in").suffix or ".bin")
            staged.append(src)
            wav = dest / "audio.wav"
            fields["audio_bytes"] = media.audio_to_wav16k(src, wav, MAX_AUDIO_SEC)
            fields["audio_ms"] = media.duration_ms(wav)
    except media.MediaError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    finally:
        for path in staged:
            path.unlink(missing_ok=True)

    store.finalize(msg_id, **fields)
    row = store.get(msg_id)
    return {"id": msg_id, "ts": row["ts"], **fields}


# ------------------------------------------------------------ device side

def _describe(row) -> dict:
    msg_id = int(row["id"])
    out: dict = {
        "id": msg_id,
        "ts": row["ts"],
        "sender": row["sender"],
        "photo": None,
        "audio": None,
    }
    if row["photo_bytes"]:
        out["photo"] = {
            "path": f"/api/v1/media/{msg_id}/photo.rgb565",
            "bytes": int(row["photo_bytes"]),
        }
    if row["audio_bytes"]:
        out["audio"] = {
            "path": f"/api/v1/media/{msg_id}/audio.wav",
            "bytes": int(row["audio_bytes"]),
            "ms": int(row["audio_ms"] or 0),
        }
    return out


@app.get("/api/v1/inbox", dependencies=[Auth])
async def inbox(since: int = 0, limit: int = 20):
    rows = store.since("inbound", since, min(limit, 50))
    return {
        "messages": [_describe(r) for r in rows],
        "latest": int(rows[-1]["id"]) if rows else since,
    }


@app.get("/api/v1/media/{msg_id}/{name}", dependencies=[Auth])
async def get_media(msg_id: int, name: str, request: Request):
    if name not in {"photo.rgb565", "audio.wav", "audio.m4a", "preview.jpg"}:
        raise HTTPException(404, "no such media")
    path = DATA / str(msg_id) / name
    if not path.is_file():
        raise HTTPException(404, "not found")
    if name in ("audio.wav", "audio.m4a"):
        store.mark_seen(msg_id)
    # FileResponse handles Range for us, which is what lets the device
    # stream audio in chunks instead of holding a whole file in PSRAM.
    return FileResponse(
        path,
        media_type={
            "photo.rgb565": "application/octet-stream",
            "audio.wav": "audio/wav",
            "audio.m4a": "audio/mp4",
            "preview.jpg": "image/jpeg",
        }[name],
    )


@app.post("/api/v1/reply", status_code=201, dependencies=[Auth])
async def reply(request: Request, x_duration_ms: int = Header(default=0)):
    """From the device: a raw 16 kHz mono WAV body, no multipart wrapper."""
    body = await request.body()
    if len(body) < 64:
        raise HTTPException(400, "empty audio body")

    msg_id = store.create("reply", sender="kid")
    dest = store.msg_dir(msg_id)
    wav = dest / "audio.wav"
    wav.write_bytes(body)

    m4a = dest / "audio.m4a"
    try:
        media.wav_to_m4a(wav, m4a)
    except media.MediaError:
        m4a = None  # push the WAV instead; not worth failing the upload over

    ms = x_duration_ms or media.duration_ms(wav)
    store.finalize(msg_id, audio_bytes=len(body), audio_ms=ms)
    await _push_to_phone(msg_id, m4a or wav, ms)
    return {"id": msg_id, "ms": ms}


async def _push_to_phone(msg_id: int, audio: Path, ms: int) -> None:
    if not NTFY_TOPIC:
        return
    seconds = max(1, round(ms / 1000))
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.put(
                f"{NTFY_SERVER}/{NTFY_TOPIC}",
                content=audio.read_bytes(),
                headers={
                    "Title": "New voice message",
                    "Message": f"{seconds} second message for you",
                    "Tags": "wave",
                    "Priority": "high",
                    "Filename": audio.name,
                },
            )
    except Exception:
        # A failed push must never fail the child's upload. She pressed the
        # button, the message is stored; delivery is a separate concern.
        pass


# ------------------------------------------------------------- web app

def _nocache(path: Path, media_type: str) -> FileResponse:
    """The shell must never be cached: a stale index.html or service worker on
    a phone is very hard to clear, and this app is useless offline anyway."""
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/", include_in_schema=False)
async def index():
    return _nocache(STATIC / "index.html", "text/html")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    return _nocache(STATIC / "sw.js", "application/javascript")


@app.get("/app.webmanifest", include_in_schema=False)
async def manifest():
    return _nocache(STATIC / "app.webmanifest", "application/manifest+json")


@app.get("/icon.svg", include_in_schema=False)
async def icon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/api/v1/health")
async def health():
    return {"ok": True, "messages": store.count()}


@app.get("/api/v1/replies", dependencies=[Auth])
async def replies(since: int = 0, limit: int = 20):
    rows = store.latest("reply", since, min(limit, 50))
    return {
        "messages": [_describe(r) for r in rows],
        "latest": int(rows[-1]["id"]) if rows else since,
    }
