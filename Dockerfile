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

# 运行时只需要这些库（不需要 build-essential / pkg-config 编译工具），
# 但保留 -dev 包名以避免因 Debian 版本不同导致的 .so 版本号包名不匹配问题。
# 这些包里既包含 .so 也包含头文件，多出来的体积只是头文件，不大。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfftw3-dev \
    libyaml-dev \
    libtag1-dev \
    libsamplerate0-dev \
    libchromaprint-dev \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    libswresample-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从构建阶段复制已安装好的 Python 包
COPY --from=builder /install /usr/local

COPY app.py .

ENV MUSIC_ROOT=/music
ENV PORT=8000
ENV MAX_ANALYSIS_SECONDS=300

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]