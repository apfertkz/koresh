# Сайдкар WhatsApp: зависимости демона ставим в отдельной стадии, в итоговый образ идут только node и node_modules.
# Версии закреплены: package.json (Baileys ровно 7.0.0-rc14) + package-lock.json, ставится npm ci. --omit=peer: sharp и
# прочие необязательные пиры нужны только для картинок, мы шлём один текст.
FROM node:22-slim AS wa
WORKDIR /wa
COPY wa/package.json wa/package-lock.json ./
RUN npm ci --omit=dev --omit=peer --no-audit --no-fund

FROM python:3.11-slim

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MEMORY_DIR=/data

RUN useradd -m -u 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=wa /usr/local/bin/node /usr/local/bin/node
COPY --from=wa /wa/node_modules /app/wa/node_modules
COPY wa/wa-daemon.js wa/package.json /app/wa/
COPY config.py activation.py notify.py modules.py memory.py usage.py iot.py prompts.py honesty.py tts.py history.py summarize.py brain.py wa.py setupwiz.py setupwiz.html setupmod.html main.py ./
# Долгая память: MEMORY_DIR. На Railway сюда монтируется Volume (mount path /data); без Volume каталог просто
# пустой и живёт до перезапуска. Volume монтируется от root: на Railway нужна переменная RAILWAY_RUN_UID=0 (см. SETUP.md).
# Сессия WhatsApp лежит там же: /data/wa/auth (права 0700 ставит сам сервер).
RUN mkdir -p /data && chown app:app /data
USER app

# Railway подставляет PORT сам
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
