# ---------- 构建阶段 ----------
FROM python:3.10-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libfftw3-dev \
    libyaml-dev \
    libtag1-dev \
    libsamplerate0-dev \
    libchromaprint-dev \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    libswresample-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# 将依赖安装到独立目录，方便后续复制
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---------- 运行阶段 ----------
FROM python:3.10-slim

# 运行时只需要动态链接库，不需要 -dev 头文件和编译工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfftw3-3 \
    libyaml-0-2 \
    libtag1v5 \
    libsamplerate0 \
    libchromaprint1 \
    libavcodec59 \
    libavformat59 \
    libavutil57 \
    libswresample4 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从构建阶段复制已安装好的 Python 包
COPY --from=builder /install /usr/local

COPY app.py .

ENV MUSIC_ROOT=/music
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]