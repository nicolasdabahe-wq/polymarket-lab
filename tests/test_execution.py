import asyncio

import pytest

from pmbot.data.clob import BookLevel, OrderBook
from pmbot.db import connect
from pmbot.execution.paper import PaperBroker, simulate_book_fill
from pmbot.risk import OrderRequest, RiskManager


def levels(*pairs):
    return [BookLevel(p, s) for p, s in pairs]


def test_fill_walks_the_book_with_slippage():
    asks = levels((0.50, 10), (0.52, 10), (0.60, 100))
    filled, avg = simulate_book_fill(asks, 20, limit_price=0.55, side="BUY")
    assert filled == 20
    assert avg == pytest.approx(0.51)


def test_fill_stops_at_limit_price():
    asks = levels((0.50, 10), (0.70, 100))
    filled, avg = simulate_book_fill(asks, 50, limit_price=0.55, side="BUY")
    assert filled == 10 and avg == pytest.approx(0.50)


def test_sell_walks_bids_down():
    bids = levels((0.48, 10), (0.45, 10), (0.30, 100))
    filled, avg = simulate_book_fill(bids, 20, limit_price=0.40, side="SELL")
    assert filled == 20
    assert avg == pytest.approx(0.465)


def test_empty_book():
    assert simulate_book_fill([], 10, 0.5, "BUY") == (0.0, 0.0)


class FakeClob:
    """CLOB falso con books fijos por token."""
    def __init__(self, books):
        self.books = books

    async def order_book(self, token_id):
        return self.books[token_id]


def make_broker(tmp_path, books, starting=500.0):
    conn = connect(tmp_path / "test.db")
    risk = RiskManager(conn, {"min_order_usdc": 1.0}, tmp_path)
    broker = PaperBroker(conn, FakeClob(books), risk,
                         {"paper_starting_usdc": starting})
    return conn, broker


def buy_req(**kw):
    base = dict(strategy="copy_trading", condition_id="0xm1",
                category="politics", token_id="t1", outcome="Yes",
                outcome_index=0, side="BUY", size=20.0, price=0.55,
                reason="test", strategy_budget_pct=0.50)
    base.update(kw)
    return OrderRequest(**base)


BOOKS = {"t1": OrderBook("t1",
                         bids=[BookLevel(0.48, 100)],
                         asks=[BookLevel(0.50, 100)])}


def test_buy_updates_cash_and_position(tmp_path):
    conn, broker = make_broker(tmp_path, BOOKS)
    fill = asyncio.run(broker.execute("o1", buy_req()))
    assert fill.status == "FILLED"
    assert broker.cash == pytest.approx(500 - 20 * 0.50)
    [pos] = broker.positions()
    assert pos["size"] == 20 and pos["avg_price"] == pytest.approx(0.50)


def test_idempotency_same_order_id(tmp_path):
    conn, broker = make_broker(tmp_path, BOOKS)
    asyncio.run(broker.execute("o1", buy_req()))
    dup = asyncio.run(broker.execute("o1", buy_req()))
    assert dup.status == "DUPLICATE"
    assert broker.cash == pytest.approx(500 - 10.0)  # no se duplicó el gasto


def test_sell_realizes_pnl(tmp_path):
    conn, broker = make_broker(tmp_path, BOOKS)
    asyncio.run(broker.execute("o1", buy_req()))
    fill = asyncio.run(broker.execute(
        "o2", buy_req(side="SELL", size=20.0, price=0.40)))
    assert fill.status == "FILLED"
    assert fill.realized_pnl == pytest.approx(20 * (0.48 - 0.50))
    assert broker.positions() == []


def test_rejected_by_risk_records_order(tmp_path):
    conn, broker = make_broker(tmp_path, BOOKS, starting=5.0)
    fill = asyncio.run(broker.execute("o1", buy_req(size=100, price=0.5)))
    assert fill.status == "REJECTED"
    row = conn.execute("SELECT * FROM orders WHERE id='o1'").fetchone()
    assert row["status"] == "REJECTED" and row["reject_reason"]


def test_min_shares_rejected(tmp_path):
    conn, broker = make_broker(tmp_path, BOOKS)
    fill = asyncio.run(broker.execute("o1", buy_req(size=3.0)))
    assert fill.status == "REJECTED" and "mínimo" in fill.detail


def test_redeem_settles_position(tmp_path):
    conn, broker = make_broker(tmp_path, BOOKS)
    asyncio.run(broker.execute("o1", buy_req()))
    [pos] = broker.positions()
    fill = broker.redeem(pos, 1.0, "resuelto YES")
    assert fill.realized_pnl == pytest.approx(20 * (1.0 - 0.50))
    assert broker.cash == pytest.approx(500 - 10 + 20)
    assert broker.positions() == []
