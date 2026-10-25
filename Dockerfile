# Python 3.13: chessfen (screenshot recognition) requires it.
FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends stockfish libcairo2 fonts-dejavu-core git \
 && rm -rf /var/lib/apt/lists/*

ENV STOCKFISH_PATH=/usr/games/stockfish \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts ./scripts
RUN python scripts/download_model.py
COPY app ./app
COPY static ./static

RUN useradd --system --no-create-home appuser \
 && mkdir -p /data/profiles && chown appuser /data/profiles
USER appuser
# Calibration profiles (tiny piece-shape files). Mount a volume here to keep them: -v bmc-profiles:/data/profiles
ENV PROFILE_DIR=/data/profiles
VOLUME /data/profiles

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
