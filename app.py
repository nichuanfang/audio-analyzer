import logging
import os
from pathlib import Path
import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache

import essentia.standard as es
import uvicorn
from fastapi import FastAPI, HTTPException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("music-analyzer")

# 配置参数
MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/music")).resolve()
PORT = int(os.getenv("PORT", 8000))
MAX_ANALYSIS_SECONDS = 200  # 适合NAS性能，降低CPU负担
CACHE_SIZE = 128  # 内存缓存大小，根据NAS内存调整

logger.info("MUSIC_ROOT = %s", MUSIC_ROOT)
logger.info("MAX_ANALYSIS_SECONDS = %d", MAX_ANALYSIS_SECONDS)

# 全局进程池
executor: ProcessPoolExecutor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor
    # Startup
    executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 2)
    logger.info("ProcessPoolExecutor 已启动，workers=%d", executor._max_workers)
    yield
    # Shutdown
    if executor:
        executor.shutdown(wait=True)
        logger.info("ProcessPoolExecutor 已关闭")


app = FastAPI(lifespan=lifespan)


@lru_cache(maxsize=CACHE_SIZE)
def extract_features(audio_path: str) -> dict:
    """CPU密集型特征提取函数（带缓存）"""
    logger.info("开始提取特征: %s", audio_path)

    try:
        loader = es.EasyLoader(
            filename=audio_path,
            sampleRate=44100,
            startTime=0,
            endTime=MAX_ANALYSIS_SECONDS,
        )
        audio_vector = loader()
        logger.info("音频加载完成，采样点数: %d", len(audio_vector))

        if len(audio_vector) == 0:
            raise ValueError("音频内容为空或无法解码")

        duration = len(audio_vector) / 44100
        if duration < 10:
            logger.warning("音频过短（%.1f秒），特征提取准确性可能降低", duration)

        # 节奏提取（multifeature 精度较高但较慢）
        rhythm = es.RhythmExtractor2013(method="multifeature")
        bpm, _, bpm_conf, _, _ = rhythm(audio_vector)

        # 舞曲度
        dance = es.Danceability()(audio_vector)
        danceability = float(dance[0]) if hasattr(dance, "__getitem__") else float(dance)

        # 调式提取
        key_ext = es.KeyExtractor()
        key, scale, key_conf = key_ext(audio_vector)

        result = {
            "bpm": round(float(bpm), 2),
            "bpm_confidence": round(float(bpm_conf), 2),
            "danceability": round(danceability, 2),
            "key": str(key),
            "scale": str(scale),
            "key_confidence": round(float(key_conf), 2),
            "analysis_duration": round(duration, 1),
        }

        logger.info("特征提取完成: %s", result)
        return result

    except Exception as e:
        logger.exception("特征提取过程中发生错误: %s", audio_path)
        raise


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    logger.info("收到分析请求: %s", audio_path)

    # 路径解析与安全校验
    try:
        full_path = (MUSIC_ROOT / audio_path.lstrip("/")).resolve(strict=False)

        # 严格路径穿越防护
        if not str(full_path).startswith(str(MUSIC_ROOT)):
            logger.warning("非法路径尝试: %s", full_path)
            raise HTTPException(status_code=403, detail="非法路径")

        if not full_path.is_file():
            logger.warning("文件不存在: %s", full_path)
            raise HTTPException(status_code=404, detail="文件不存在")
    except Exception as e:
        logger.warning("路径处理异常: %s", str(e))
        raise HTTPException(status_code=400, detail="路径无效")

    logger.info("文件有效，开始分析: %s", full_path)

    try:
        # 在进程池中执行CPU密集任务
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            executor, extract_features, str(full_path)
        )

        return {
            "status": "success",
            "data": result,
            "file": audio_path
        }
    except Exception as e:
        logger.error("分析失败: %s", full_path)
        raise HTTPException(
            status_code=500,
            detail="音乐特征分析失败，请检查日志或稍后重试"
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)