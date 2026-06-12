from fastapi import FastAPI, HTTPException
import essentia.standard as es
import os
import uvicorn

app = FastAPI()

# 容器内挂载的根目录
MUSIC_ROOT = os.getenv("MUSIC_ROOT", "/music")
PORT = int(os.getenv("PORT", 8000))


def extract_features(audio_path):
    loader = es.EasyLoader(filename=audio_path, sampleRate=44100, startTime=0, endTime=360)
    audio_vector = loader()

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
        "key_confidence": round(float(key_conf), 2)
    }


@app.get("/analyze/{audio_path:path}")
async def analyze(audio_path: str):
    # 拼接出容器内的绝对路径
    full_path = os.path.join(MUSIC_ROOT, audio_path)

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {full_path}")

    try:
        result = extract_features(full_path)
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"特征分析失败: {str(e)}")


if __name__ == "__main__":
    # 使用环境变量中的端口启动
    uvicorn.run(app, host="0.0.0.0", port=PORT)