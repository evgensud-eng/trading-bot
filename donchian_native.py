"""
donchian_native.py — НАТИВНЫЙ дневной расчёт Donchian BTC/ETH внутри бота.
Назначение: убрать зависимость от TradingView-вебхука для donchian_daily / donchian_eth.

⚠️ EXECUTION-PATH, НЕ смена параметров (§7.10).
Логика 1-в-1 с Pine-исходниками (источник истины, §1):
  • donchian_daily_v1.1.pine  (BTC)
  • donchian_eth_v1_1.pine     (ETH)
Параметры ЗАМОРОЖЕНЫ: entry_lookback=40, exit_lookback=30, SMA200 trend filter,
BTC cooldown=5 баров, ETH без cooldown.

Источник OHLCV: Kraken через CCXT (как в Tier-1 reconciliation), BTC/USD + ETH/USD, 1d.
Запуск: APScheduler cron после закрытия дневного бара (UTC 00:05).
Выход: тот же signal-dict, что слал TV-вебхук → council_fn (run_council) → Telegram + Sheets.
Идемпотентность: эмит транзишена один раз на бар (dedup по дате закрытого бара, атомарный JSON).

Интеграция (в app.py, 3 строки):
    from donchian_native import register
    register(scheduler, run_council,
             telegram_fn=send_telegram, forward_trade_fn=log_forward_trade)

Зависимости боты уже имеют: apscheduler. Новая: ccxt (добавить в requirements.txt).
"""

import os
import json
import time
import logging
import tempfile
import datetime as dt

import ccxt

# ─── FROZEN-параметры (НЕ менять — §7.10) ────────────────────────────────────
ENTRY_LOOKBACK = 40
EXIT_LOOKBACK = 30
SMA_LEN = 200
BTC_COOLDOWN_BARS = 5          # только BTC; ETH — без cooldown
OHLCV_LIMIT = 720              # Kraken отдаёт до 720 дневных баров (хватает на SMA200 + warmup)

STATE_PATH = os.environ.get("DONCHIAN_STATE_PATH",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "donchian_native_state.json"))

# Конфиг по символам — отражает РАЗЛИЧИЯ двух Pine-версий.
STRATEGIES = {
    "donchian_daily": {
        "symbol": "BTC",
        "ccxt_pair": "BTC/USD",
        "entry_mode": "close",      # BTC: вход по close > entry_high
        "trend_close_offset": 0,    # SMA сравнивается с close ТЕКУЩЕГО бара
        "sma_offset": 0,            # sma(close,200) на текущем баре
        "cooldown": BTC_COOLDOWN_BARS,
        "exit_sl_field": "exit_low",
    },
    "donchian_eth": {
        "symbol": "ETH",
        "ccxt_pair": "ETH/USD",
        "entry_mode": "high",       # ETH: вход по high > entry_high (intra-bar)
        "trend_close_offset": 1,    # above_sma = close[1] > sma  (предыдущий close)
        "sma_offset": 1,            # sma(close[1],200) — заканчивается на предыдущем баре
        "cooldown": 0,              # ETH без cooldown
        "exit_sl_field": "zero",    # ETH exit alert шлёт sl:0 (как в Pine)
    },
}

log = logging.getLogger("donchian_native")


# ─── ДАННЫЕ ──────────────────────────────────────────────────────────────────
def fetch_daily(ccxt_pair, limit=OHLCV_LIMIT):
    """Дневные OHLCV с Kraken через CCXT. Возвращает список баров [t,o,h,l,c,v] по возрастанию.
    Последний (ещё не закрытый) дневной бар отбрасывается — считаем только по ЗАКРЫТЫМ."""
    ex = ccxt.kraken({"enableRateLimit": True})
    raw = ex.fetch_ohlcv(ccxt_pair, timeframe="1d", limit=limit)
    raw.sort(key=lambda r: r[0])
    # ccxt ts в мс. Сегодняшний (формирующийся) бар = дата UTC == сегодня.
    today = dt.datetime.now(dt.timezone.utc).date()
    if raw and dt.datetime.fromtimestamp(raw[-1][0] / 1000, dt.timezone.utc).date() == today:
        raw = raw[:-1]
    return raw


def _bar_date(bar):
    return dt.datetime.fromtimestamp(bar[0] / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


# ─── СИМУЛЯЦИЯ (детерминированная, Pine 1-в-1) ───────────────────────────────
def simulate(strategy_key, candles):
    """Полный прогон логики по закрытым барам → текущая позиция + последний транзишен.
    Re-simulate каждый тик: state самоисцеляется, дрейфа нет.
    Возвращает dict с текущими уровнями (на последнем закрытом баре), позицией и событием бара."""
    cfg = STRATEGIES[strategy_key]
    H = [c[2] for c in candles]
    L = [c[3] for c in candles]
    C = [c[4] for c in candles]
    n = len(candles)
    warmup = SMA_LEN + ENTRY_LOOKBACK + 1
    if n < warmup:
        raise ValueError(f"{strategy_key}: мало баров ({n} < {warmup})")

    pos = 0
    entry_price = entry_i = None
    last_exit_i = -10 ** 9
    min_low = None
    last_event = None  # (date, type, price, sl)

    for i in range(warmup, n):
        entry_high = max(H[i - ENTRY_LOOKBACK:i])      # highest(high,40)[1] → бары i-40..i-1
        exit_low = min(L[i - EXIT_LOOKBACK:i])         # lowest(low,30)[1]   → бары i-30..i-1

        if cfg["sma_offset"] == 0:                     # BTC: sma(close,200) на текущем баре
            sma = sum(C[i - SMA_LEN + 1:i + 1]) / SMA_LEN
        else:                                          # ETH: sma(close[1],200) до предыдущего бара
            sma = sum(C[i - SMA_LEN:i]) / SMA_LEN

        trend_close = C[i] if cfg["trend_close_offset"] == 0 else C[i - 1]
        above_trend = trend_close > sma

        if cfg["entry_mode"] == "close":
            breakout = C[i] > entry_high
        else:                                          # ETH: intra-bar high
            breakout = H[i] > entry_high

        cooldown_ok = (i - last_exit_i) >= cfg["cooldown"]

        if pos == 0:
            if breakout and above_trend and cooldown_ok:
                pos, entry_price, entry_i, min_low = 1, C[i], i, L[i]
                sl = exit_low if cfg["exit_sl_field"] == "exit_low" else 0
                last_event = (_bar_date(candles[i]), "LONG", C[i], exit_low)
        else:
            min_low = min(min_low, L[i])
            if L[i] <= exit_low if cfg["entry_mode"] == "close" else L[i] < exit_low:
                # BTC: low <= exit_low ; ETH: low < exit_low (см. Pine)
                sl = exit_low if cfg["exit_sl_field"] == "exit_low" else 0
                last_event = (_bar_date(candles[i]), "EXIT", C[i], sl)
                pos, entry_price, entry_i, last_exit_i = 0, None, None, i

    last = n - 1
    eh = max(H[last - ENTRY_LOOKBACK:last])
    el = min(L[last - EXIT_LOOKBACK:last])
    if cfg["sma_offset"] == 0:
        sma_last = sum(C[last - SMA_LEN + 1:last + 1]) / SMA_LEN
    else:
        sma_last = sum(C[last - SMA_LEN:last]) / SMA_LEN

    res = {
        "strategy": strategy_key,
        "symbol": cfg["symbol"],
        "bar_date": _bar_date(candles[last]),
        "close": C[last],
        "entry_high": eh,
        "exit_low": el,
        "sma200": sma_last,
        "above_sma": (C[last] if cfg["trend_close_offset"] == 0 else C[last - 1]) > sma_last,
        "position": pos,
        "entry_price": entry_price,
        "last_event": last_event,
        "posture": "IN_POSITION" if pos else "FLAT",
    }
    if pos and entry_price:
        res["unreal_pnl_pct"] = round((C[last] / entry_price - 1) * 100, 2)
        res["max_drawdown_pct"] = round((min_low / entry_price - 1) * 100, 2)
    return res


# ─── СОСТОЯНИЕ (атомарный JSON, дедуп эмита) ─────────────────────────────────
def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state):
    d = os.path.dirname(STATE_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _emit_signal(strategy_key, event):
    """event = (bar_date, type, price, sl) → signal-dict, идентичный TV-вебхуку."""
    cfg = STRATEGIES[strategy_key]
    bar_date, typ, price, sl = event
    return {
        "strategy": strategy_key,
        "symbol": cfg["symbol"],
        "type": typ,                       # LONG | EXIT
        "price": round(float(price), 2),
        "sl": round(float(sl), 2),
        "source": "native_daily",          # отличает от source=tv-webhook
        "bar_date": bar_date,
    }


# ─── ТИК (вызывается планировщиком) ──────────────────────────────────────────
def run_tick(council_fn, telegram_fn=None, forward_trade_fn=None):
    """Один дневной проход по обеим Donchian-стратегиям после закрытия бара.
    council_fn(signal) — существующий run_council (тот же путь, что и вебхук).
    Эмитит ENTRY/EXIT ровно один раз на бар; HOLD — только в лог."""
    state = _load_state()
    summary = []
    for skey in STRATEGIES:
        try:
            candles = fetch_daily(STRATEGIES[skey]["ccxt_pair"])
            r = simulate(skey, candles)
        except Exception as e:
            log.error("donchian_native %s: ошибка расчёта: %s", skey, e)
            if telegram_fn:
                telegram_fn(f"⚠️ donchian_native {skey}: ошибка расчёта — {e}")
            continue

        ev = r["last_event"]
        st = state.get(skey, {})
        fired = None
        # Эмитим, только если транзишен случился НА последнем закрытом баре и ещё не эмитили.
        if ev and ev[0] == r["bar_date"] and st.get("last_emitted_bar") != ev[0]:
            signal = _emit_signal(skey, ev)
            log.info("donchian_native %s: ЭМИТ %s @ %s", skey, signal["type"], signal["price"])
            try:
                council_fn(signal)          # → Telegram + Sheets внутри (как вебхук)
                if forward_trade_fn and signal["type"] == "EXIT":
                    forward_trade_fn(strategy=STRATEGIES[skey]["symbol"],
                                     entry_time="", exit_time=signal["bar_date"],
                                     side="long", entry_price="", exit_price=signal["price"],
                                     qty="", pnl="", reason="native 30d breakdown exit")
            except Exception as e:
                log.error("donchian_native %s: council/log ошибка: %s", skey, e)
            st["last_emitted_bar"] = ev[0]
            fired = signal["type"]
        else:
            log.info("donchian_native %s: HOLD/%s bar=%s close=%.2f eH=%.2f eL=%.2f sma=%.2f aboveSMA=%s",
                     skey, r["posture"], r["bar_date"], r["close"],
                     r["entry_high"], r["exit_low"], r["sma200"], r["above_sma"])

        st["last_bar"] = r["bar_date"]
        st["position"] = r["position"]
        state[skey] = st
        summary.append({**{k: r[k] for k in
                           ("strategy", "bar_date", "close", "entry_high", "exit_low",
                            "sma200", "above_sma", "posture")},
                        "fired": fired})
    state["updated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _save_state(state)
    return summary


# ─── РЕГИСТРАЦИЯ В ПЛАНИРОВЩИКЕ ──────────────────────────────────────────────
def register(scheduler, council_fn, telegram_fn=None, forward_trade_fn=None,
             hour=0, minute=5):
    """Повесить дневной Donchian-тик на существующий APScheduler.
    UTC 00:05 — через 5 минут после закрытия дневного бара Kraken (00:00 UTC)."""
    scheduler.add_job(
        lambda: run_tick(council_fn, telegram_fn, forward_trade_fn),
        "cron", hour=hour, minute=minute, timezone="UTC",
        id="donchian_native_daily", replace_existing=True,
    )
    log.info("donchian_native: джоба зарегистрирована (cron UTC %02d:%02d)", hour, minute)


# ─── САМОТЕСТ / ВАЛИДАЦИЯ ────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("=== donchian_native самотест (живые данные Kraken) ===")
    for skey in STRATEGIES:
        candles = fetch_daily(STRATEGIES[skey]["ccxt_pair"])
        r = simulate(skey, candles)
        print(f"\n[{skey}] {r['symbol']}  bars={len(candles)}  закрытый бар={r['bar_date']}")
        print(f"  close      = {r['close']:.2f}")
        print(f"  entry_high(40)[1] = {r['entry_high']:.2f}  -> breakout: {r['close']>r['entry_high']}")
        print(f"  exit_low(30)[1]   = {r['exit_low']:.2f}")
        print(f"  sma200            = {r['sma200']:.2f}  above_sma: {r['above_sma']}")
        print(f"  ПОЗИЦИЯ: {r['posture']}  (pos={r['position']})  last_event={r['last_event']}")
        if r.get("unreal_pnl_pct") is not None:
            print(f"  P&L={r['unreal_pnl_pct']}%  maxDD={r['max_drawdown_pct']}%")
