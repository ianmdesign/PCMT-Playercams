FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/app/config \
    DATA_DIR=/data \
    PORT=5400

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app/app
COPY config /app/config
RUN mkdir -p /data

EXPOSE 5400
CMD ["sh", "-c", "uvicorn app.main:application --host 0.0.0.0 --port ${PORT}"]
