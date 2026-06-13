import asyncio
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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import ORJSONResponse

# =========================
# Logging (主进程专用)
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
MAX_ANALYSIS_SECONDS = int(os.getenv("MAX_ANALYSIS_SECONDS", "360"))

# 动态计算核心数，留足余量给主进程
MAX_WORKERS = min(max((os.cpu_count() or 2) // 2, 1), 4)

executor: ProcessPoolExecutor | None = None
analysis_semaphore = asyncio.Semaphore(MAX_WORKERS)


# =========================
# CACHE (轻量级 LRU 缓存实现，免去外部依赖)
# =========================

class LRUCache:
    def __init__(self, capacity: int = 5000):
        self.capacity = capacity
        self.cache: Dict[str, Any] = {}

    def get(self, key: str):
        if key not in self.cache:
            return None
        # 移动到末尾表示最近使用
        val = self.cache.pop(key)
        self.cache[key] = val
        return val

    def set(self, key: str, value: Any):
        if key in self.cache:
            self.cache.pop(key)
        elif len(self.cache) >= self.capacity:
            # 弹出最早放入的项（最久未使用）
            iter_keys = iter(self.cache.keys())
            first_key = next(iter_keys)
            self.cache.pop(first_key)
        self.cache[key] = value

    def __len__(self):
        return len(self.cache)


CACHE = LRUCache(capacity=10000)


# =========================
# Feature extraction (CPU bound - 子进程执行)
# =========================

def extract_features(audio_path: str, max_seconds: int) -> dict:
    """
    此函数在单独的子进程中运行。
    注意：为了避免多进程死锁，绝对不能在此函数内调用主进程的 logging 模块。
    """
    t0 = time.monotonic()

    # 1. 加载音频
    audio = es.EasyLoader(
        filename=audio_path,
        sampleRate=44100,
        startTime=0,
        endTime=max_seconds,
    )()

    if len(audio) == 0:
        raise ValueError("Audio file is empty or corrupted")

    duration = len(audio) / 44100

    # 2. 提取节奏与 BPM
    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, _, bpm_conf, *_ = rhythm(audio)

    # 3. 提取舞曲度 (Essentia 范围通常在 0 ~ 3 之间)
    dance = es.Danceability()(audio)
    dance = float(dance[0]) if isinstance(dance, tuple) else float(dance)

    # 4. 提取调性
    key, scale, key_conf = es.KeyExtractor()(audio)

    cost_ms = (time.monotonic() - t0) * 1000

    return {
        "bpm": round(float(bpm), 2),
        "bpm_confidence": round(float(bpm_conf), 2),
        "danceability": round(dance, 2),
        "key": str(key),
        "scale": str(scale),
        "key_confidence": round(float(key_conf), 2),
        "duration": round(duration, 1),
        "_internal_cost_ms": round(cost_ms, 2)  # 传回主进程打印
    }


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
        log("RES", "🌐", "done", id=request_id, status=response.status_code, cost_ms=round(cost, 2))
        return response
    except Exception as e:
        cost = (time.monotonic() - start) * 1000
        log("ERR", "❌", "failed", id=request_id, cost_ms=round(cost, 2), error=str(e))
        raise


# =========================
# FS 安全检查
# =========================

def resolve_audio_path(rel: str) -> Path:
    full = (MUSIC_ROOT / rel.lstrip("/")).resolve()

    try:
        full.relative_to(MUSIC_ROOT)
    except ValueError:
        raise HTTPException(403, "Illegal path: Access denied")

    if not full.exists():
        raise HTTPException(404, f"Audio file not found: {rel}")

    return full


# =========================
# API Endpoints
# =========================

@app.get("/health")
async def health():
    return {"status": "ok", "cache_size": len(CACHE)}


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    full = resolve_audio_path(audio_path)
    key = str(full)

    log("API", "🌐", "analyze request", file=audio_path)

    # 1. 命中缓存路径
    cached = CACHE.get(key)
    if cached:
        log("CACHE", "🧠", "hit", file=audio_path)
        return {
            "status": "success",
            "source": "cache",
            "file": audio_path,
            "data": cached,
        }

    log("CACHE", "🧠", "miss", file=audio_path)

    # 2. 进入进程池计算路径
    async with analysis_semaphore:
        loop = asyncio.get_running_loop()
        log("AUDIO", "🎧", "start compute", file=audio_path)

        try:
            # 传参时显式带入 MAX_ANALYSIS_SECONDS，避免子进程读取不到环境变量
            result = await loop.run_in_executor(
                executor,
                extract_features,
                key,
                MAX_ANALYSIS_SECONDS
            )

            # 提取内部耗时并将其从返回数据中剥离
            internal_cost = result.pop("_internal_cost_ms", 0)
            log("AUDIO", "🎧", "done",
                bpm=result["bpm"],
                key=f"{result['key']} {result['scale']}",
                compute_cost_ms=internal_cost)

        except ValueError as ve:
            # 捕获音频文件空或损坏的明确异常
            log("WARN", "⚠️", "invalid audio", file=audio_path, error=str(ve))
            raise HTTPException(status_code=400, detail=str(ve))
        except Exception as e:
            # 捕获其他不可预知的底层崩溃
            log("ERR", "❌", "process crashed", file=audio_path, error=str(e))
            raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    # 3. 写入缓存并返回
    CACHE.set(key, result)

    return {
        "status": "success",
        "source": "compute",
        "file": audio_path,
        "data": result,
    }


# =========================
# Main Entry
# =========================

if __name__ == "__main__":
    MUSIC_ROOT.mkdir(parents=True, exist_ok=True)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        access_log=False,  # 已经有自定义中间件接管日志，关闭默认日志提升速度
    )