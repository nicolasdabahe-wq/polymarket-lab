# pmbot — bot de trading para Polymarket

Bot modular en Python para operar en Polymarket 24/7. **Arranca siempre en
paper trading**; el modo real requiere una variable de entorno explícita.

## Estado actual (fase 1 — solo lectura)

| Módulo | Estado | Qué hace |
|---|---|---|
| `pmbot/data/` | ✅ | Mercados activos (Gamma), order books (CLOB), posiciones/actividad/leaderboard (Data API), cache SQLite |
| `pmbot/smart_money/` | ✅ (lectura) | Ranking de wallets con score compuesto y filtros anti-insider; snapshot de posiciones; detección de trades nuevos |
| `pmbot/intel/` | ✅ (lectura) | 10 feeds RSS configurables, mapeo noticia→mercado (Claude API con fallback por keywords), briefing diario por categoría |
| `pmbot/scheduler/` | ✅ | Rutina diaria a hora fija + polls intradía; reporte diario a `reports/` y Telegram |
| `pmbot/monitor/` | ✅ parcial | Logging estructurado (JSON opcional), notificaciones Telegram |
| `strategies/`, `risk/`, `execution/`, `research/`, `backtest/` | 🔜 fases 2–3 | Definidos en `config.yaml` (límites de riesgo) pero sin ejecutar órdenes |

En fase 1 **ninguna orden se envía**, ni siquiera simulada: el bot observa,
rankea, analiza y reporta. Las señales se registran en la tabla `signals`.

## Instalación local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # completar lo que se quiera usar (todo opcional en fase 1)
```

## Comandos

```bash
python -m pmbot markets        # refresca cache de mercados y resume por categoría
python -m pmbot rank-wallets   # ranking de wallets top (leaderboard + score propio)
python -m pmbot positions      # posiciones actuales de las 5 mejores wallets
python -m pmbot positions 0x…  # posiciones de una wallet específica
python -m pmbot news           # baja feeds y analiza noticias nuevas
python -m pmbot briefing       # briefing diario por categoría
python -m pmbot daily          # rutina diaria completa, una vez
python -m pmbot run            # loop 24/7 (lo que corre Docker)
```

## Despliegue en VPS con Docker

```bash
cp .env.example .env   # editar
docker compose up -d --build
docker compose logs -f pmbot
docker compose exec pmbot python -m pmbot briefing   # inspección puntual
```

La base SQLite vive en el volumen `pmbot-data`; sobrevive reinicios.

## Configuración

- **`.env`** — secretos y switches de entorno (nunca commitear): claves,
  Telegram, `LIVE_TRADING`.
- **`config.yaml`** — parámetros: feeds RSS, pesos y filtros del scoring de
  wallets, horario de la rutina diaria, límites de riesgo, modelo LLM.

### LLM (opcional)

Con `ANTHROPIC_API_KEY` en `.env`, `intel/` usa Claude para resumir cada
noticia, mapearla a mercados y estimar dirección/impacto. Sin clave, degrada
a matching por keywords (mapea mercados pero sin dirección estimada).
`intel.llm.max_news_per_run` limita el gasto por corrida.

### Telegram (opcional)

Crear un bot con @BotFather, poner `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`
en `.env` y `telegram.enabled: true` en `config.yaml`. El reporte diario y
las alertas llegan al chat.

## Paper → real

1. Fase 1–2 corren en paper por diseño; no hay código de órdenes reales aún.
2. Cuando exista `execution/` (fase 3), el modo real exigirá **exactamente**:

   ```
   LIVE_TRADING=I_UNDERSTAND_THE_RISKS
   POLYMARKET_PRIVATE_KEY=...
   POLYMARKET_PROXY_ADDRESS=...
   ```

   Cualquier otro valor de `LIVE_TRADING` (incluidos `1`, `true`, `yes`)
   deja el bot en paper. Esta regla tiene tests (`tests/test_config.py`) y
   toda orden pasará por los límites duros de `risk/`.

## Tests

```bash
python -m pytest tests/ -q
```

Cubren: la regla paper/real, el scoring y filtros de wallets, el mapeo
noticia→mercado y la categorización de mercados / horario del scheduler.

## Arquitectura (resumen)

```
pmbot/
├── config.py        .env + config.yaml; regla LIVE_TRADING fail-safe
├── db.py            esquema SQLite (WAL) compartido
├── http.py          HTTP async con reintentos, backoff y Retry-After
├── context.py       cableado de módulos (build_app)
├── data/            gamma.py · clob.py · data_api.py · store.py
├── intel/           sources.py (RSS) · analyzer.py (LLM/keywords) · briefing.py
├── smart_money/     ranking.py (score) · tracker.py (posiciones y señales)
├── scheduler/       daily.py (rutina diaria + loop 24/7)
└── monitor/         logs.py · notify.py (Telegram)
```

Flujo diario: mercados → ranking wallets → posiciones top → noticias →
análisis → briefing → reporte (`reports/YYYY-MM-DD.md` + Telegram).
