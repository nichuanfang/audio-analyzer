import logging
import os
from pathlib import Path

import essentia.standard as es
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("music-analyzer")

app = FastAPI()

# 容器内挂载的根目录
MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/music")).resolve()
PORT = int(os.getenv("PORT", 8000))

logger.info("MUSIC_ROOT = %s", MUSIC_ROOT)


def extract_features(audio_path: str) -> dict:
    logger.info("开始提取特征: %s", audio_path)

    loader = es.EasyLoader(
        filename=audio_path, sampleRate=44100, startTime=0, endTime=360
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

    # 拼接并解析出容器内的绝对路径
    full_path = (MUSIC_ROOT / audio_path).resolve()
    logger.info("解析后的完整路径: %s", full_path)

    # 防止路径遍历：确保最终路径仍位于 MUSIC_ROOT 内
    try:
        full_path.relative_to(MUSIC_ROOT)
    except ValueError:
        logger.warning("非法路径(超出 MUSIC_ROOT): %s", full_path)
        raise HTTPException(status_code=403, detail="非法路径")

    if not full_path.is_file():
        logger.warning("文件不存在: %s", full_path)
        # 额外打印父目录下的文件列表,辅助排查编码问题
        parent = full_path.parent
        if parent.is_dir():
            try:
                entries = os.listdir(parent)
                logger.info("父目录 %s 下的文件: %s", parent, entries)
            except Exception as e:
                logger.warning("无法列出父目录: %s", e)
        else:
            logger.warning("父目录也不存在: %s", parent)
        raise HTTPException(status_code=404, detail=f"文件不存在: {audio_path}")

    logger.info("文件存在,开始分析: %s", full_path)

    try:
        result = await run_in_threadpool(extract_features, str(full_path))
        logger.info("分析成功: %s", full_path)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.exception("特征分析失败: %s", full_path)
        raise HTTPException(status_code=500, detail=f"特征分析失败: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)