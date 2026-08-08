FROM python:3.13-alpine

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/battery_tracker.db

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py schema.sql ./
COPY templates ./templates
COPY static ./static

RUN addgroup -S battery && adduser -S battery -G battery && mkdir /data && chown battery:battery /data
USER battery

EXPOSE 8000
VOLUME ["/data"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "app:app"]
