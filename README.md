# pmbot — bot de trading para Polymarket

Bot modular en Python para operar en Polymarket 24/7. **Arranca siempre en
paper trading**; el modo real requiere una variable de entorno explícita.

## Estado actual (fase 2 — paper trading)

| Módulo | Estado | Qué hace |
|---|---|---|
| `pmbot/data/` | ✅ | Mercados activos (Gamma), order books (CLOB), posiciones/actividad/leaderboard (Data API), cache SQLite |
| `pmbot/smart_money/` | ✅ | Ranking de wallets con score compuesto y filtros anti-insider; snapshot de posiciones; señales por trades nuevos |
| `pmbot/intel/` | ✅ | 10 feeds RSS configurables, mapeo noticia→mercado (Claude API con fallback por keywords), briefing diario por categoría |
| `pmbot/risk/` | ✅ | Límites duros: % máx por mercado/categoría/wallet copiada/estrategia, exposición total, stop diario, kill switch. Ninguna orden sale sin pasar por acá |
| `pmbot/execution/` | ✅ paper | Broker simulado: fills contra el order book real del CLOB (camina niveles → slippage realista), idempotencia por orden, posiciones y PnL en SQLite, redeem al resolver |
| `pmbot/strategies/` | ✅ arb + copy | Arbitraje YES+NO<1 (verificado contra el CLOB, con unwind si una pata no llena) y copy trading (consenso de wallets o entrada fuerte de una top, límite de slippage, salida cuando la wallet sale) |
| `pmbot/scheduler/` | ✅ | Rutina diaria (settle → salidas → entradas → reporte) + copia intradía en tiempo real y escaneo de arbitraje |
| `pmbot/monitor/` | ✅ parcial | Logging estructurado, Telegram. Dashboard web: fase 3 |
| `research/`, `news_trading`, `backtest/`, broker real | 🔜 fase 3 | |

**Todo es simulado**: el dinero es virtual (`capital.paper_starting_usdc`),
pero los precios, books, slippage y resoluciones son reales. Si un día no hay
oportunidades que superen los umbrales, el bot NO opera y lo reporta.

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
python -m pmbot trade-cycle    # un ciclo de trading paper (settle/salidas/entradas)
python -m pmbot portfolio      # equity, posiciones abiertas y PnL
python -m pmbot trades         # últimas órdenes con motivo (llenadas y rechazadas)
python -m pmbot kill on|off    # kill switch manual: bloquea compras nuevas
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

1. La fase 2 corre en paper por diseño; el broker real no existe todavía.
2. **Antes de pasar a real**: correr el paper 2–4 semanas mínimo y revisar el
   PnL por estrategia en los reportes diarios. Solo pasar a real si hay
   ventaja consistente, y con menos capital del que uno está dispuesto a perder.
3. Cuando exista el broker real (fase 3), el modo real exigirá **exactamente**:

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
├── risk/            manager.py (límites duros + kill switch)
├── execution/       paper.py (broker simulado contra books reales)
├── strategies/      arbitrage.py · copy_trading.py
├── scheduler/       daily.py (rutina diaria + loop 24/7)
└── monitor/         logs.py · notify.py (Telegram)
```

Flujo diario: mercados → ranking wallets → posiciones top → noticias →
briefing → settle resueltos → salidas (tesis rota) → entradas nuevas
(si pasan risk/) → reporte (`reports/YYYY-MM-DD.md` + Telegram).
Flujo intradía: señal smart_money → copia en tiempo real; refresh de
mercados → escaneo de arbitraje.
