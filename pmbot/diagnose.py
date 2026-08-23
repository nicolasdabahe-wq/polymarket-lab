import asyncio, json, sqlite3
from datetime import datetime, timedelta, timezone
from pmbot.config import load_config
from pmbot.context import build_app
from pmbot.strategies.copy_trading import (pick_candidates, pick_holdings_consensus,
                                           market_not_started, _outcome_index, slippage_ok)

async def diagnose() -> None:
    app = build_app(load_config())
    conn = app.conn
    cfg = app.copy_trading.cfg
    print("="*64)
    print("DIAGNÓSTICO DEL EMBUDO DE TRADING")
    print("="*64)

    # 0) LATIDO: distingue "el bot está muerto o ciego" de "está mirando
    #    pero nada pasa los filtros". Es la primera pregunta a responder
    #    cuando lleva horas sin operar.
    ahora = datetime.now(timezone.utc)

    def _hace(iso: str | None) -> str:
        if not iso:
            return "nunca"
        try:
            t = datetime.fromisoformat(iso)
        except ValueError:
            return iso
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        mins = (ahora - t).total_seconds() / 60
        return (f"hace {mins:.0f} min" if mins < 120
                else f"hace {mins/60:.1f} h")

    ult_sig = conn.execute(
        "SELECT MAX(created_at) t FROM signals WHERE source='smart_money'"
    ).fetchone()["t"]
    # Un intento NO es una compra: la orden queda registrada aunque el
    # riesgo la rechace o el CLOB la tumbe. Contarlas juntas hacía parecer
    # que el bot operaba cuando en realidad se estaba estrellando.
    ult_ord = conn.execute(
        "SELECT MAX(created_at) t FROM orders WHERE side='BUY' "
        "AND status='FILLED'").fetchone()["t"]
    h2 = (ahora - timedelta(hours=2)).isoformat()
    sig_2h = conn.execute(
        "SELECT COUNT(*) c FROM signals WHERE source='smart_money' "
        "AND created_at >= ?", (h2,)).fetchone()["c"]
    llenas_2h = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE side='BUY' AND status='FILLED' "
        "AND created_at >= ?", (h2,)).fetchone()["c"]
    intentos_2h = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE side='BUY' AND created_at >= ?",
        (h2,)).fetchone()["c"]
    print(f"\n0) LATIDO")
    print(f"   última señal vista : {_hace(ult_sig)}  ({sig_2h} en 2h)")
    print(f"   última COMPRA REAL : {_hace(ult_ord)}  "
          f"({llenas_2h} llenadas de {intentos_2h} intentos en 2h)")
    if sig_2h == 0:
        print("   ⚠️  NO llegan señales: el problema es de vigilancia "
              "(bot caído, API o wallets sin actividad), no de filtros.")
    elif intentos_2h == 0:
        print("   → Llegan señales pero ninguna llega a mandarse. "
              "El motivo de cada descarte está en los logs ('no copio').")
    elif llenas_2h == 0:
        print("   ⚠️  Se INTENTA comprar y no entra ninguna: mirá los "
              "motivos de rechazo de la sección 4.")

    scores = app.copy_trading._wallet_scores()
    thr = app.copy_trading._min_usdc_by_wallet()
    frenadas = app.copy_trading._wallets_frenadas_en_vivo()
    print(f"\n1) WALLETS COPIABLES: {len(scores)}")
    print(f"   umbral de tamaño por wallet: {sorted(set(thr.values()))}")
    if frenadas:
        print(f"   ⛔ frenadas por pérdida real (≥${app.copy_trading.live_stop_usdc:.0f}): "
              f"{len(frenadas)} → {', '.join(w[:10] for w in sorted(frenadas))}")

    # señales
    since = (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
    rows = conn.execute("SELECT payload, condition_id, processed FROM signals WHERE source='smart_money' AND created_at>=?", (since,)).fetchall()
    payloads=[]
    for r in rows:
        p=json.loads(r["payload"])
        p["condition_id"]=p.get("condition_id") or r["condition_id"]
        payloads.append(p)
    de_copiables=[p for p in payloads if p.get("wallet") in scores]
    buys=[p for p in de_copiables if p.get("side")=="BUY"]
    print(f"\n2) SEÑALES (24h): {len(rows)} totales | {len(de_copiables)} de wallets copiables | {len(buys)} son compras")
    if buys:
        tam = sorted((p.get("usdc",0) for p in buys), reverse=True)[:8]
        print(f"   tamaños más grandes: {[round(t) for t in tam]}")
        cands = pick_candidates(buys, scores, cfg, thr)
        print(f"   → candidatas que disparan copia: {len(cands)}")
        # cuántas mueren por umbral
        pasa_umbral=[p for p in buys if p.get("usdc",0) >= thr.get(p.get("wallet"), float(cfg.get("min_copy_usdc_of_wallet",150)))]
        print(f"   → pasan el umbral de tamaño: {len(pasa_umbral)}")

    # consenso
    hc = cfg.get("holdings_consensus") or {}
    holdings=[{"wallet":r["wallet"],"condition_id":r["condition_id"],"outcome":r["outcome"] or "",
               "value":r["value_usdc"] or 0.0,"avg_price":r["avg_price"] or 0.0}
              for r in conn.execute("SELECT * FROM wallet_positions") if r["wallet"] in scores]
    print(f"\n3) CONSENSO — posiciones de wallets copiables: {len(holdings)}")
    grupos = pick_holdings_consensus(holdings, hc)
    print(f"   grupos con >={hc.get('min_wallets')} wallets y >=${hc.get('min_value_usdc')}: {len(grupos)}")
    razones={"no_en_cache":0,"categoria":0,"en_vivo":0,"outcome":0,"precio_alto":0,"slippage":0,"OK":0}
    allowed=set(hc.get("categories") or [])
    for c in grupos:
        m = conn.execute("SELECT * FROM markets WHERE condition_id=? AND active=1",(c["condition_id"],)).fetchone()
        if not m: razones["no_en_cache"]+=1; continue
        if m["category"] not in allowed: razones["categoria"]+=1; continue
        if m["category"]=="sports" and hc.get("sports_only_prematch",True) and not market_not_started(m):
            razones["en_vivo"]+=1; continue
        idx=_outcome_index(m,c["outcome"])
        if idx is None: razones["outcome"]+=1; continue
        cur=app.broker.mark_price(c["condition_id"],idx,c["avg_entry"])
        if cur > float(hc.get("max_entry_price",0.90)): razones["precio_alto"]+=1; continue
        if not slippage_ok(c["avg_entry"],cur,app.copy_trading.max_slippage): razones["slippage"]+=1; continue
        razones["OK"]+=1
        print(f"   ✅ OPORTUNIDAD: {m['question'][:50]} | {c['outcome']} @ {cur:.3f} ({len(c['wallets'])} wallets)")
    print("   descartes:", {k:v for k,v in razones.items() if v})

    # riesgo
    st=app.broker.portfolio_state()
    print(f"\n4) CAPITAL: equity ${st.equity:.2f} | cash ${st.cash:.2f} | expuesto ${st.exposure_total:.2f}")
    ct = app.copy_trading
    print(f"   tamaño por copia (Kelly): entre ${ct.min_trade_usdc:.2f} y "
          f"${st.equity*ct.max_trade_pct:.2f}")
    print(f"   stop diario: equity inicio del día ${st.day_start_equity:.2f}"
          + (" ⛔ ACTIVADO" if st.day_start_equity > 0 and st.equity <=
             st.day_start_equity * (1 - app.risk.limits.daily_stop_loss_pct)
             else ""))

    # Presupuesto por estrategia: el techo se mide contra el equity de HOY,
    # así que cuando el equity baja el techo baja con él y una estrategia
    # puede quedar por encima de su propio límite sin haber comprado nada.
    # Ese caso bloquea todas sus compras hasta que sus posiciones cierren, y
    # hasta ahora solo se veía como un rechazo suelto en los logs.
    presupuestos = load_config().section("strategies") or {}
    print("\n5) PRESUPUESTO POR ESTRATEGIA (techo = % del equity de hoy)")
    for nombre, scfg in sorted(presupuestos.items()):
        pct = float((scfg or {}).get("budget_pct", 0.0))
        if pct <= 0:
            continue
        usado = st.exposure_by_strategy.get(nombre, 0.0)
        techo = pct * st.equity
        libre = techo - usado
        marca = "⛔ SIN CUPO" if libre <= ct.min_trade_usdc else "✅"
        print(f"   {marca} {nombre:<16} ${usado:6.2f} de ${techo:6.2f} "
              f"({pct:.0%}) → libre ${libre:+7.2f}")
    ordenes=conn.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status").fetchall()
    print(f"   órdenes por estado: {[(r['status'],r['c']) for r in ordenes]}")
    # Los motivos van COMPLETOS: recortados a 40 caracteres escondían justo
    # la parte que importa de los errores del CLOB (el status y el cuerpo),
    # que es lo único que dice por qué no entró una orden.
    rech = conn.execute(
        """SELECT reject_reason, COUNT(*) c FROM orders
           WHERE reject_reason IS NOT NULL
           GROUP BY reject_reason ORDER BY c DESC LIMIT 8""").fetchall()
    if rech:
        print("\n6) POR QUÉ SE RECHAZARON LAS ÓRDENES (histórico)")
        for r in rech:
            print(f"   {r['c']:>3}x  {r['reject_reason']}")
    ultimos = conn.execute(
        """SELECT created_at, status, reject_reason, reason FROM orders
           WHERE side = 'BUY' ORDER BY created_at DESC LIMIT 5""").fetchall()
    if ultimos:
        print("\n7) LOS 5 ÚLTIMOS INTENTOS DE COMPRA")
        for r in ultimos:
            marca = "✅" if r["status"] == "FILLED" else "❌"
            print(f"   {marca} {_hace(r['created_at']):>14}  {r['status']}"
                  + (f"  {r['reject_reason']}" if r["reject_reason"] else ""))
            print(f"      {(r['reason'] or '')[:100]}")
    await app.aclose()

if __name__ == "__main__":
    asyncio.run(diagnose())
