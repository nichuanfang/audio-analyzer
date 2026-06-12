FROM python:3.10-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config \
    libfftw3-dev libyaml-dev libsamplerate0-dev \
    libtag1-dev libavcodec-dev libavformat-dev libavutil-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels

COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir -r requirements.txt


FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libfftw3-3 libyaml-0-2 libsamplerate0 libtag1v5 \
    libavcodec59 libavformat59 libavutil57 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels

RUN pip install --no-cache-dir /wheels/*

COPY app.py .

ENV MUSIC_ROOT=/music
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]