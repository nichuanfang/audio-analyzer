import logging
import os
from pathlib import Path
import asyncio
from concurrent.futures import ProcessPoolExecutor

import essentia.standard as es
import uvicorn
from fastapi import FastAPI, HTTPException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("music-analyzer")

app = FastAPI()

MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/music")).resolve()
PORT = int(os.getenv("PORT", 8000))

logger.info("MUSIC_ROOT = %s", MUSIC_ROOT)

# 创建全局ProcessPoolExecutor，建议调整max_workers为CPU核数或适合的值
executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 2)


def extract_features(audio_path: str) -> dict:
    logger.info("开始提取特征: %s", audio_path)

    loader = es.EasyLoader(
        filename=audio_path, sampleRate=44100, startTime=0, endTime=200
    )
    audio_vector = loader()
    logger.info("音频加载完成,采样点数: %d", len(audio_vector))

    if len(audio_vector) == 0:
        raise ValueError("音频内容为空或无法解码")

    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, _, bpm_conf, _, _ = rhythm(audio_vector)
    logger.info("节奏提取完成: bpm=%.2f, conf=%.2f", bpm, bpm_conf)

    dance = es.Danceability()(audio_vector)
    logger.info("舞曲度提取完成: %.2f", dance[0])

    key_ext = es.KeyExtractor()
    key, scale, key_conf = key_ext(audio_vector)
    logger.info("调式提取完成: key=%s, scale=%s, conf=%.2f", key, scale, key_conf)

    result = {
        "bpm": round(float(bpm), 2),
        "bpm_confidence": round(float(bpm_conf), 2),
        "danceability": round(float(dance[0]), 2),
        "key": str(key),
        "scale": str(scale),
        "key_confidence": round(float(key_conf), 2),
    }
    logger.info("特征提取结果: %s", result)
    return result


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    logger.info("收到请求, audio_path=%r", audio_path)

    # 解析出绝对路径
    full_path = (MUSIC_ROOT / audio_path).resolve()
    logger.info("解析后的完整路径: %s", full_path)

    # 路径安全校验
    try:
        full_path.relative_to(MUSIC_ROOT)
    except ValueError:
        logger.warning("非法路径(超出 MUSIC_ROOT): %s", full_path)
        raise HTTPException(status_code=403, detail="非法路径")

    if not full_path.is_file():
        logger.warning("文件不存在: %s", full_path)
        try:
            parent_files = os.listdir(full_path.parent) if full_path.parent.is_dir() else []
            logger.info("父目录 %s 下的文件: %s", full_path.parent, parent_files)
        except Exception as e:
            logger.warning("父目录无法访问: %s", e)
        raise HTTPException(status_code=404, detail=f"文件不存在: {audio_path}")

    logger.info("文件存在，开始分析: %s", full_path)

    loop = asyncio.get_running_loop()
    try:
        # 使用进程池并行执行CPU密集型任务
        result = await loop.run_in_executor(executor, extract_features, str(full_path))
        logger.info("分析成功: %s", full_path)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.exception("特征分析失败: %s", full_path)
        raise HTTPException(status_code=500, detail=f"特征分析失败: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)