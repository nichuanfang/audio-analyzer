import os
from pathlib import Path

import essentia.standard as es
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

app = FastAPI()

# 容器内挂载的根目录
MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/music")).resolve()
PORT = int(os.getenv("PORT", 8000))


def extract_features(audio_path: str) -> dict:
    loader = es.EasyLoader(
        filename=audio_path, sampleRate=44100, startTime=0, endTime=360
    )
    audio_vector = loader()

    if len(audio_vector) == 0:
        raise ValueError("音频内容为空或无法解码")

    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, _, bpm_conf, _, _ = rhythm(audio_vector)

    dance = es.Danceability()(audio_vector)

    key_ext = es.KeyExtractor()
    key, scale, key_conf = key_ext(audio_vector)

    return {
        "bpm": round(float(bpm), 2),
        "bpm_confidence": round(float(bpm_conf), 2),
        "danceability": round(float(dance[0]), 2),
        "key": str(key),
        "scale": str(scale),
        "key_confidence": round(float(key_conf), 2),
    }


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    # 拼接并解析出容器内的绝对路径
    full_path = (MUSIC_ROOT / audio_path).resolve()

    # 防止路径遍历：确保最终路径仍位于 MUSIC_ROOT 内
    try:
        full_path.relative_to(MUSIC_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="非法路径")

    if not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {audio_path}")

    try:
        result = await run_in_threadpool(extract_features, str(full_path))
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"特征分析失败: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)