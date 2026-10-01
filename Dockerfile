# Боевой образ бота для Render (Docker).
# Обратите внимание: whisper-модель скачивается при первом запуске (в runtime).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/workspace/hf

# ffmpeg + зависимости для faster-whisper/ctranslate2
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# зависимости сначала (кэширование слоёв)
COPY requirements.txt ./
RUN pip install -r requirements.txt

# код
COPY src ./src

# рабочая папка для видео/моделей/состояния
RUN mkdir -p /workspace

EXPOSE 8443

ENV VT_POLLING=1 \
    VT_WHISPER_MODEL=small \
    VT_DEVICE=cpu \
    VT_COMPUTE=int8

# PYTHONPATH указывает на код, лежащий в /app/src
ENV PYTHONPATH=/app/src

CMD ["python", "-m", "main"]