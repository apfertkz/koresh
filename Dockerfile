FROM python:3.11-slim

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MEMORY_DIR=/data

RUN useradd -m -u 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.py activation.py memory.py usage.py iot.py prompts.py honesty.py tts.py history.py summarize.py brain.py setupwiz.py setupwiz.html main.py ./
# Долгая память: MEMORY_DIR. На Railway сюда монтируется Volume (mount path /data); без Volume каталог просто
# пустой и живёт до перезапуска. Volume монтируется от root: на Railway нужна переменная RAILWAY_RUN_UID=0 (см. SETUP.md).
RUN mkdir -p /data && chown app:app /data
USER app

# Railway подставляет PORT сам
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
