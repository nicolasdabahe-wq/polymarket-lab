FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home pmbot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY pmbot/ pmbot/

USER pmbot
ENV PMBOT_VAR_DIR=/data \
    PMBOT_LOG_JSON=1

# Loop 24/7 en paper trading. El modo real requiere LIVE_TRADING en el .env.
CMD ["python", "-m", "pmbot", "run"]
