FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    WHISPER_MODEL=small \
    WHISPER_DEVICE=cpu \
    WHISPER_COMPUTE_TYPE=int8 \
    WHISPER_PRELOAD=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the Whisper model into the image so a restart does not download ~500 MB again.
ARG WHISPER_MODEL=small
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"
# Language ID on the first partial (LanguageId, ~0.3 s).
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

COPY conversate ./conversate

# Railway sets PORT.
CMD ["sh", "-c", "uvicorn conversate.main:app --factory --host 0.0.0.0 --port ${PORT:-8000} --ws-ping-interval 15 --ws-ping-timeout 20"]
