import asyncio
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import essentia.standard as es
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import ORJSONResponse


# =========================
# Logging
# =========================

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("music-analyzer")


def log(tag: str, icon: str, msg: str, **fields):
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info(f"{icon} [{tag}] {msg} {extra}".rstrip())


# =========================
# Config
# =========================

MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "./music")).resolve()
PORT = int(os.getenv("PORT", "8000"))

MAX_ANALYSIS_SECONDS = 200
MAX_WORKERS = min(max((os.cpu_count() or 2) // 2, 1), 4)

executor: ProcessPoolExecutor | None = None
analysis_semaphore = asyncio.Semaphore(MAX_WORKERS)

# ⚠️ multiprocessing-safe cache（必须显式，不用 decorator）
CACHE: dict[str, dict] = {}


# =========================
# Lifespan
# =========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor
    executor = ProcessPoolExecutor(max_workers=MAX_WORKERS)
    log("POOL", "⚙️", "started", workers=MAX_WORKERS)
    yield
    executor.shutdown(wait=True)
    log("POOL", "⚙️", "stopped")


app = FastAPI(
    title="Music Analyzer",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)


# =========================
# Middleware (trace + latency)
# =========================

@app.middleware("http")
async def trace(request: Request, call_next):
    request_id = uuid.uuid4().hex[:8]
    start = time.monotonic()

    log("REQ", "🌐", "start", id=request_id, path=request.url.path)

    try:
        response = await call_next(request)
        cost = (time.monotonic() - start) * 1000

        log("RES", "🌐", "done",
            id=request_id,
            status=response.status_code,
            cost_ms=round(cost, 2))

        return response

    except Exception:
        cost = (time.monotonic() - start) * 1000
        log("ERR", "❌", "failed", id=request_id, cost_ms=round(cost, 2))
        raise


# =========================
# FS
# =========================

def resolve_audio_path(rel: str) -> Path:
    full = (MUSIC_ROOT / rel.lstrip("/")).resolve()

    try:
        full.relative_to(MUSIC_ROOT)
    except ValueError:
        raise HTTPException(403, "illegal path")

    if not full.exists():
        raise HTTPException(404, "not found")

    return full


# =========================
# CACHE (correct semantics)
# =========================

def cache_get(key: str):
    return CACHE.get(key)


def cache_set(key: str, value: dict):
    CACHE[key] = value


# =========================
# Feature extraction (CPU bound)
# =========================

def extract_features(audio_path: str) -> dict:
    log("AUDIO", "🎧", "start", file=audio_path)

    t0 = time.monotonic()

    audio = es.EasyLoader(
        filename=audio_path,
        sampleRate=44100,
        startTime=0,
        endTime=MAX_ANALYSIS_SECONDS,
    )()

    if len(audio) == 0:
        raise ValueError("empty audio")

    duration = len(audio) / 44100

    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, _, bpm_conf, *_ = rhythm(audio)

    dance = es.Danceability()(audio)
    dance = float(dance[0]) if isinstance(dance, tuple) else float(dance)

    key, scale, key_conf = es.KeyExtractor()(audio)

    result = {
        "bpm": round(float(bpm), 2),
        "bpm_confidence": round(float(bpm_conf), 2),
        "danceability": round(dance, 2),
        "key": str(key),
        "scale": str(scale),
        "key_confidence": round(float(key_conf), 2),
        "duration": round(duration, 1),
    }

    cost = (time.monotonic() - t0) * 1000

    log("AUDIO", "🎧", "done",
        bpm=result["bpm"],
        key=result["key"],
        cost_ms=round(cost, 2))

    return result


# =========================
# API
# =========================

@app.get("/health")
async def health():
    return {"status": "ok", "cache_size": len(CACHE)}


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    full = resolve_audio_path(audio_path)
    key = str(full)

    log("API", "🌐", "analyze", file=key)

    # 1. cache hit path
    cached = cache_get(key)
    if cached:
        log("CACHE", "🧠", "hit", file=key)
        return {
            "status": "success",
            "source": "cache",
            "file": audio_path,
            "data": cached,
        }

    log("CACHE", "🧠", "miss", file=key)

    # 2. compute path
    async with analysis_semaphore:
        loop = asyncio.get_running_loop()

        result = await loop.run_in_executor(
            executor,
            extract_features,
            key,
        )

    cache_set(key, result)

    return {
        "status": "success",
        "source": "compute",
        "file": audio_path,
        "data": result,
    }


# =========================
# main
# =========================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        access_log=False,
    )