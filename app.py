import asyncio
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import essentia.standard as es
import uvicorn
from cachetools import TTLCache, cached
from fastapi import FastAPI, HTTPException
from fastapi.responses import ORJSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("music-analyzer")

# =========================
# Config
# =========================

MUSIC_ROOT = Path(
    os.getenv("MUSIC_ROOT", "/music")
).resolve()

PORT = int(os.getenv("PORT", "8000"))

MAX_ANALYSIS_SECONDS = 200

MAX_WORKERS = min(
    max((os.cpu_count() or 2) // 2, 1),
    4,
)

CACHE_SIZE = 256
CACHE_TTL = 86400  # 24h

logger.info("MUSIC_ROOT=%s", MUSIC_ROOT)
logger.info("MAX_WORKERS=%s", MAX_WORKERS)

# =========================
# Runtime
# =========================

executor: ProcessPoolExecutor | None = None

feature_cache = TTLCache(
    maxsize=CACHE_SIZE,
    ttl=CACHE_TTL,
)

analysis_semaphore = asyncio.Semaphore(MAX_WORKERS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor

    executor = ProcessPoolExecutor(
        max_workers=MAX_WORKERS
    )

    logger.info(
        "ProcessPoolExecutor started workers=%s",
        MAX_WORKERS,
    )

    yield

    if executor:
        executor.shutdown(wait=True)
        logger.info("ProcessPoolExecutor stopped")


app = FastAPI(
    title="Music Analyzer",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)


# =========================
# Feature Extraction
# =========================

@cached(feature_cache)
def extract_features(audio_path: str) -> dict:
    logger.info("Analyzing: %s", audio_path)

    loader = es.EasyLoader(
        filename=audio_path,
        sampleRate=44100,
        startTime=0,
        endTime=MAX_ANALYSIS_SECONDS,
    )

    audio = loader()

    if len(audio) == 0:
        raise ValueError("Empty audio")

    duration = len(audio) / 44100

    rhythm = es.RhythmExtractor2013(
        method="multifeature"
    )

    rhythm_result = rhythm(audio)

    bpm = float(rhythm_result[0])
    bpm_conf = float(rhythm_result[2])

    dance_result = es.Danceability()(audio)

    if isinstance(dance_result, tuple):
        danceability = float(dance_result[0])
    elif hasattr(dance_result, "__getitem__"):
        danceability = float(dance_result[0])
    else:
        danceability = float(dance_result)

    key_result = es.KeyExtractor()(audio)

    key = str(key_result[0])
    scale = str(key_result[1])
    key_conf = float(key_result[2])

    return {
        "bpm": round(bpm, 2),
        "bpm_confidence": round(bpm_conf, 2),
        "danceability": round(danceability, 2),
        "key": key,
        "scale": scale,
        "key_confidence": round(key_conf, 2),
        "analysis_duration": round(duration, 1),
    }


# =========================
# Helpers
# =========================

def resolve_audio_path(
    relative_path: str,
) -> Path:
    full_path = (
        MUSIC_ROOT / relative_path.lstrip("/")
    ).resolve()

    try:
        full_path.relative_to(MUSIC_ROOT)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Illegal path",
        )

    if not full_path.exists():
        raise HTTPException(
            status_code=404,
            detail="File not found",
        )

    if not full_path.is_file():
        raise HTTPException(
            status_code=400,
            detail="Not a file",
        )

    return full_path


# =========================
# API
# =========================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "cache_size": len(feature_cache),
        "workers": MAX_WORKERS,
    }


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    full_path = resolve_audio_path(
        audio_path
    )

    logger.info(
        "Request analysis: %s",
        str(full_path),
    )

    async with analysis_semaphore:
        try:
            loop = asyncio.get_running_loop()

            result = await loop.run_in_executor(
                executor,
                extract_features,
                str(full_path),
            )

            return {
                "status": "success",
                "file": audio_path,
                "data": result,
            }

        except HTTPException:
            raise

        except Exception:
            logger.exception(
                "Analysis failed: %s",
                str(full_path),
            )

            raise HTTPException(
                status_code=500,
                detail="Analysis failed",
            )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )