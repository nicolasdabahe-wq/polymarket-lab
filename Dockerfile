FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home pmbot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY pmbot/ pmbot/

# El volumen de datos debe pertenecer al usuario del bot, no a root;
# si no, SQLite no puede escribir y el contenedor entra en crash-loop.
RUN mkdir -p /data /app/reports && chown -R pmbot:pmbot /data /app/reports

USER pmbot
ENV PMBOT_VAR_DIR=/data \
    PMBOT_LOG_JSON=1

# Loop 24/7 en paper trading. El modo real requiere LIVE_TRADING en el .env.
CMD ["python", "-m", "pmbot", "run"]
