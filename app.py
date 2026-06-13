import asyncio
import json
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Any

import essentia.standard as es
import uvicorn
from cachetools import LRUCache
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import ORJSONResponse

# =========================
# Logging 配置
# =========================

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("music-analyzer")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger.setLevel(LOG_LEVEL)


def log(tag: str, icon: str, msg: str, **fields):
    """仅在允许的日志级别下输出"""
    if logger.level > logging.INFO and tag not in ("ERR", "WARN", "CACHE"):
        return
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info(f"{icon} [{tag}] {msg} {extra}".rstrip())


# =========================
# 配置
# =========================

MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "./music")).resolve()
PORT = int(os.getenv("PORT", "8000"))
MAX_ANALYSIS_SECONDS = int(os.getenv("MAX_ANALYSIS_SECONDS", "360"))
CACHE_FILE = MUSIC_ROOT / ".analysis_cache.json"

MAX_WORKERS = min(max((os.cpu_count() or 4) - 2, 2), 6)

executor: ProcessPoolExecutor | None = None
analysis_semaphore = asyncio.Semaphore(MAX_WORKERS)


# =========================
# 持久化缓存
# =========================

def load_cache() -> LRUCache:
    cache = LRUCache(maxsize=20000)
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
                for k, v in data.items():
                    if isinstance(v, dict):  # 安全检查
                        cache[k] = v
            log("CACHE", "💾", "loaded from disk", size=len(cache))
        except Exception as e:
            log("WARN", "⚠️", "cache load failed", error=str(e))
    return cache


def save_cache(cache: LRUCache):
    try:
        data = dict(cache)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        log("CACHE", "💾", "saved to disk", size=len(cache))
    except Exception as e:
        logger.warning(f"Failed to save cache: {e}")


CACHE: LRUCache = load_cache()


# =========================
# 特征提取（子进程）
# =========================

def extract_features(audio_path: str, max_seconds: int) -> dict:
    t0 = time.monotonic()
    try:
        loader = es.EasyLoader(
            filename=audio_path,
            sampleRate=44100,
            startTime=0,
            endTime=max_seconds,
        )
        audio = loader()

        if len(audio) < 44100 * 5:  # 至少5秒有效音频
            raise ValueError("Audio too short or empty")

        duration = len(audio) / 44100.0

        # BPM
        rhythm = es.RhythmExtractor2013(method="multifeature")
        bpm, _, bpm_conf, *_ = rhythm(audio)

        # Danceability
        dance = es.Danceability()(audio)
        danceability = float(dance[0]) if isinstance(dance, (list, tuple)) else float(dance)

        # Key
        key, scale, key_conf = es.KeyExtractor()(audio)

        cost_ms = (time.monotonic() - t0) * 1000

        return {
            "bpm": round(float(bpm), 2),
            "bpm_confidence": round(float(bpm_conf), 3),
            "danceability": round(danceability, 2),
            "key": str(key),
            "scale": str(scale),
            "key_confidence": round(float(key_conf), 3),
            "duration": round(duration, 1),
        }
    except Exception as e:
        raise RuntimeError(f"Feature extraction failed: {str(e)}") from e


# =========================
# Lifespan
# =========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor
    executor = ProcessPoolExecutor(max_workers=MAX_WORKERS)
    log("POOL", "⚙️", "started", workers=MAX_WORKERS)
    yield
    if executor:
        executor.shutdown(wait=True)
    save_cache(CACHE)
    log("POOL", "⚙️", "stopped")


app = FastAPI(
    title="Music Analyzer",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
    version="1.2.0"
)


# =========================
# Middleware
# =========================

@app.middleware("http")
async def trace(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    request_id = uuid.uuid4().hex[:8]
    start = time.monotonic()

    log("REQ", "🌐", "start", id=request_id, path=request.url.path)

    try:
        response = await call_next(request)
        cost = (time.monotonic() - start) * 1000
        log("RES", "🌐", "done", id=request_id, status=response.status_code, cost_ms=round(cost, 1))
        return response
    except Exception:
        cost = (time.monotonic() - start) * 1000
        log("ERR", "❌", "failed", id=request_id, cost_ms=round(cost, 1))
        raise


# =========================
# 路径安全
# =========================

def resolve_audio_path(rel: str) -> Path:
    full = (MUSIC_ROOT / rel.lstrip("/")).resolve()
    try:
        full.relative_to(MUSIC_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Illegal path: Access denied")

    if not full.is_file():
        raise HTTPException(status_code=404, detail=f"Audio file not found: {rel}")
    return full


# =========================
# API
# =========================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "cache_size": len(CACHE),
        "max_workers": MAX_WORKERS,
        "cache_file": str(CACHE_FILE),
    }


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    full_path = resolve_audio_path(audio_path)
    cache_key = str(full_path)

    # 缓存命中（静默）
    if cached := CACHE.get(cache_key):
        return {
            "status": "success",
            "source": "cache",
            "file": audio_path,
            "data": cached,
        }

    log("CACHE", "🧠", "miss", file=audio_path)

    async with analysis_semaphore:
        loop = asyncio.get_running_loop()
        try:
            result: dict = await loop.run_in_executor(
                executor, extract_features, cache_key, MAX_ANALYSIS_SECONDS
            )
            log("AUDIO", "✅", "computed", file=audio_path, bpm=result.get("bpm"), cost_ms="N/A")
        except ValueError as ve:
            log("WARN", "⚠️", "invalid audio", file=audio_path, error=str(ve))
            raise HTTPException(status_code=400, detail=str(ve))
        except Exception as e:
            log("ERR", "❌", "analysis failed", file=audio_path, error=type(e).__name__)
            raise HTTPException(status_code=500, detail="Analysis failed")

    CACHE[cache_key] = result
    return {
        "status": "success",
        "source": "compute",
        "file": audio_path,
        "data": result,
    }


# =========================
# 启动
# =========================

if __name__ == "__main__":
    MUSIC_ROOT.mkdir(parents=True, exist_ok=True)

    if not os.access(MUSIC_ROOT, os.R_OK | os.W_OK):
        logger.error("MUSIC_ROOT is not readable or writable")
        sys.exit(1)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        access_log=False,
        workers=1,
        log_level=LOG_LEVEL.lower()
    )