# Guía de despliegue — paso a paso

Esta guía te deja el bot corriendo 24/7 en tu VPS. Las secciones 1–6 valen
para paper y real; la sección 7 es el paso a dinero real (5.000 MXN ≈ 270
USDC).

## 1. Requisitos en el VPS

- Docker y Docker Compose instalados (`docker --version` y
  `docker compose version` deben responder).
- Git.

```bash
# Ubuntu/Debian, si falta Docker:
curl -fsSL https://get.docker.com | sh
```

## 2. Clonar y configurar

```bash
git clone https://github.com/nicolasdabahe-wq/polymarket-lab.git
cd polymarket-lab
git checkout claude/polymarket-trading-bot-7k30pg
cp .env.example .env
```

Editá `.env` (con `nano .env`). Para arrancar **no hace falta completar
nada** — todo es opcional en paper:

| Variable | ¿Para qué? | ¿Obligatoria? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Análisis de noticias con Claude (dirección/impacto) | No, pero muy recomendada |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Reporte diario y alertas a tu teléfono | No |
| `LIVE_TRADING` | **Dejar vacía.** Solo se usará en fase real | No |

### Telegram (5 minutos, recomendado)

1. En Telegram hablale a **@BotFather** → `/newbot` → te da un token.
2. Hablale a tu bot nuevo (mandale "hola").
3. Conseguí tu chat id hablándole a **@userinfobot** (te responde tu id).
4. Poné token e id en `.env` y en `config.yaml` cambiá
   `telegram.enabled: false` → `true`.

### Claude API (5 minutos, recomendado)

1. Crear cuenta en https://console.anthropic.com
2. Cargar un crédito chico (USD 5 alcanza para semanas con la config
   actual: máx. 20 noticias por corrida con esfuerzo bajo).
3. Crear API key y ponerla en `.env` como `ANTHROPIC_API_KEY`.

## 3. Levantar el bot

```bash
docker compose up -d --build
```

Listo. Verificá que corre:

```bash
docker compose logs -f pmbot     # Ctrl+C para salir de los logs
```

Deberías ver `scheduler iniciado [PAPER]` y la hora de la próxima rutina
diaria (11:00 UTC ≈ 5:00 AM Ciudad de México; se cambia en `config.yaml`
→ `scheduler.daily_run_utc`).

## 4. Comandos útiles del día a día

```bash
docker compose exec pmbot python -m pmbot portfolio   # equity y posiciones
docker compose exec pmbot python -m pmbot trades      # últimas órdenes y motivos
docker compose exec pmbot python -m pmbot briefing    # briefing de noticias
docker compose exec pmbot python -m pmbot rank-wallets # ranking de wallets
docker compose exec pmbot python -m pmbot kill on     # PÁNICO: frenar compras
docker compose exec pmbot python -m pmbot kill off    # reanudar
```

El reporte diario también queda en el contenedor en `reports/` y llega por
Telegram si lo configuraste.

## 5. Actualizar el bot cuando haya cambios nuevos

```bash
cd polymarket-lab
git pull
docker compose up -d --build
```

(La base de datos vive en un volumen Docker aparte: no se pierde nada.)

## 6. Qué mirar durante las próximas semanas

Cada día, en el reporte:

1. **Equity y PnL total** — la curva importa más que un día puntual.
2. **PnL por estrategia** — ¿copy trading gana? ¿el arbitraje aparece?
3. **Motivos de los movimientos** — cada orden dice por qué se hizo, y los
   días sin oportunidades lo dicen explícitamente (eso es normal y sano).

Si en algún momento algo se ve raro: `docker compose exec pmbot python -m
pmbot kill on` frena todas las compras al instante (las ventas siguen
permitidas para poder salir).

## 7. Pasar a dinero real

Pasos que solo vos podés hacer:

1. **Cuenta**: creá tu cuenta en https://polymarket.com (login con email es
   lo más simple). Verificá que te deje operar desde México.
2. **Fondear**: depositá tus 5.000 MXN convertidos a **USDC en la red
   Polygon**. Lo más fácil: comprar USDC en un exchange (Bitso, Binance…)
   y retirarlo a tu dirección de depósito de Polymarket **eligiendo la red
   Polygon**. El depósito desde la app configura los permisos (allowances)
   automáticamente.
3. **Credenciales** para el bot, en `.env` del VPS:
   - `POLYMARKET_PRIVATE_KEY` → en Polymarket: perfil → ⚙️ Settings →
     **Export Private Key**.
   - `POLYMARKET_PROXY_ADDRESS` → la dirección `0x…` de tu perfil
     (botón de copiar bajo tu nombre de usuario).
   - `POLYMARKET_SIGNATURE_TYPE=3` (cuentas de la app actual; ver
     .env.example para cuentas viejas o MetaMask).
   - `LIVE_TRADING=I_UNDERSTAND_THE_RISKS` (exactamente ese texto).
4. **Verificar sin operar**:
   ```bash
   docker compose up -d --build
   docker compose exec pmbot python -m pmbot live-check
   ```
   Debe mostrar tu dirección, tu saldo USDC y "Allowance: OK".
5. **Encender**: `docker compose restart pmbot`. A partir de ahí las
   órdenes son reales. Los límites siguen activos: máx ~10% del capital por
   mercado, stop diario del 5%, y `kill on` como freno de emergencia.
6. **Cobrar mercados resueltos**: cuando un mercado tuyo resuelve, el
   payout se reclama en la app de Polymarket (botón **Claim** en el
   portfolio). El bot te lo recuerda en el reporte.

⚠️ La clave privada da control TOTAL de tus fondos: nunca la compartas, no
la subas a GitHub (el `.gitignore` ya excluye `.env`) y no la pegues en
chats. Yo nunca te la voy a pedir.
