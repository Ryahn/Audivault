FROM python:3.12-slim

# ffmpeg provides ffprobe, used as a fallback for chapters/duration mutagen can't read.
# util-linux provides setpriv, used by the entrypoint to drop root privileges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py docker-entrypoint.sh ./
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    ABSTREAM_HOST=0.0.0.0 \
    ABSTREAM_PORT=13378 \
    ABSTREAM_DATA_DIR=/config \
    ABSTREAM_CONFIG_DIR=/config \
    ABSTREAM_METADATA_DIR=/metadata \
    PUID=1000 \
    PGID=1000

VOLUME ["/config", "/metadata"]
EXPOSE 13378

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"ABSTREAM_PORT\"]}/healthcheck', timeout=4)" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "run.py"]
