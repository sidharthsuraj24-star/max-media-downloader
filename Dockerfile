FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PORT=10000 \
    MAX_FILESIZE_MB=500 \
    JOB_TTL_SECONDS=300

# One worker so the in-memory job table is shared by every request;
# threads give concurrency. Long timeout for SSE progress streams.
CMD gunicorn app:app --workers 1 --threads 8 --timeout 0 --bind 0.0.0.0:${PORT}
