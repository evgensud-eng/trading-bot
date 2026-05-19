import os
import re
import json
import logging
import statistics
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ─── ENV ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
DEEPSEEK_API_KEY   = os.environ.get("DEEPSEEK_API_KEY", "")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SPREADSHEET_ID     = os.environ.get("SPREADSHEET_ID", "")
GS_JSON            = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# ─── STRATEGY CONFIG ────────────────────────────────────────────────────────
# Чтобы добавить новую стратегию — добавь словарь сюда. Без правок промптов и сообщений.
STRATEGY_CONFIGS = {
    "v13": {
        "name": "Mean Reversion BB",
        "description": "Mean Reversion on Bollinger Bands lower band, BTC/USDT 1H",
        "tf": "1H",
        "type": "contrarian",
        "ai_rules": "BUY only if R/R >= 1.5 and RSI < 45 and signal is LONG. Otherwise SKIP.",
        "uses_consilium": True,
        "uses_tp_rr": True,
        "uses_rsi": True
    },
    "donchian_daily": {
        "name": "Donchian Breakout",
        "description": "Donchian Channel Breakout (Turtle System) on BTC/USDT Daily. Entry on close above 40-day high, exit on 30-day low. Above SMA200 trend filter.",
        "tf": "1D",
        "type": "trend_following",
        "ai_rules": "BUY if entry confirmed by Daily close above 40d high AND above SMA200 (trend OK) AND stop distance < 15% from entry. This is trend-following — high RSI is NORMAL (don't reject for overbought). Reject only if stop is unreasonably far or trend filter failed. Otherwise SKIP.",
        "uses_consilium": True,
        "uses_tp_rr": False,
        "uses_rsi": False
    }
}

def get_strategy_config(strategy_key):
    return STRATEGY_CONFIGS.get(strategy_key, STRATEGY_CONFIGS["v13"])

# ─── GOOGLE SHEETS ──────────────────────────────────────────────────────────
def get_sheet():
    try:
        creds_dict = json.loads(GS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        return client.open_by_key(SPREADSHEET_ID).sheet1
    except Exception as e:
        logging.error(f"Sheets подключение: {e}")
        return None

def ensure_header(sheet):
    try:
        if not sheet.row_values(1):
            headers = [
                "Дата", "Время", "Символ", "Тип",
                "Цена", "SL", "TP", "R/R", "RSI",
                "Claude", "DeepSeek", "GPT", "Gemini",
                "Голоса", "Решение", "Стратегия", "Причина SKIP"
            ]
            sheet.append_row(headers)
    except Exception as e:
        logging.error(f"Ошибка заголовка: {e}")

def log_to_sheets(signal, votes, decision, buy_count, total_votes, skip_reason=""):
    sheet = get_sheet()
    if sheet is None:
        return False
    ensure_header(sheet)
    now = datetime.now()
    row = [
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        signal.get("symbol", "BTC"),
        signal.get("type", "LONG"),
        signal.get("price", ""),
        signal.get("sl", ""),
        signal.get("tp", ""),
        signal.get("rr", ""),
        signal.get("rsi", ""),
        votes.get("claude", "SKIP"),
        votes.get("deepseek", "SKIP"),
        votes.get("gpt", "SKIP"),
        votes.get("gemini", "N/A"),
        f"{buy_count}/{total_votes}",
        decision,
        signal.get("strategy", "v13"),
        skip_reason if decision == "SKIP" else ""
    ]
    try:
        sheet.append_row(row)
        logging.info(f"Sheets: записано — {decision}")
        return True
    except Exception as e:
        logging.error(f"Sheets append_row: {e}")
        return False

# ─── ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ─────────────────────────────────────────────────────
# Индексы колонок (фиксированы порядком log_to_sheets):
# 0=Дата 1=Время 2=Символ 3=Тип 4=Цена 5=SL 6=TP 7=R/R 8=RSI
# 9=Claude 10=DeepSeek 11=GPT 12=Gemini 13=Голоса 14=Решение 15=Стратегия 16=Причина SKIP
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

def _parse_sheet_rows(sheet):
    """Читает все строки листа, возвращает только валидные строки с данными.
    Использует get_all_values() вместо get_all_records() — не зависит от заголовков.
    Строка валидна если col[0] = дата YYYY-MM-DD и длина >= 15."""
    all_values = sheet.get_all_values()
    return [r for r in all_values if len(r) >= 15 and _DATE_RE.match(str(r[0]))]

def weekly_report():
    logging.info("Генерирую еженедельный отчёт...")
    sheet = get_sheet()
    if sheet is None:
        send_telegram("⚠️ Еженедельный отчёт: Sheets недоступен")
        return
    try:
        data_rows = _parse_sheet_rows(sheet)
        if not data_rows:
            send_telegram("📊 Еженедельный отчёт: данных пока нет")
            return

        week_ago   = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        week_rows  = [r for r in data_rows if r[0] >= week_ago]
        total      = len(week_rows)
        buy_count  = sum(1 for r in week_rows if r[14] == "BUY")
        skip_count = total - buy_count

        reason_counts = {}
        for r in week_rows:
            reason = r[16] if len(r) > 16 else ""
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        reason_text = "\n".join([f"   • {r}: {c}x" for r, c in top_reasons]) if top_reasons else "   нет данных"

        strategies = {}
        for r in week_rows:
            s = r[15] if len(r) > 15 and r[15] else "v13"
            strategies[s] = strategies.get(s, 0) + 1
        strat_text = "\n".join([f"   {k}: {v}" for k, v in strategies.items()])

        msg = (
            f"📊 <b>Еженедельный отчёт</b>\n"
            f"📅 {week_ago} → {datetime.now().strftime('%Y-%m-%d')}\n\n"
            f"📡 Всего сигналов: <b>{total}</b>\n"
            f"🟢 BUY: <b>{buy_count}</b>\n"
            f"⚪ SKIP: <b>{skip_count}</b>\n\n"
            f"📈 По стратегиям:\n{strat_text}\n\n"
            f"❌ Топ причины SKIP:\n{reason_text}\n\n"
            f"🔗 <a href='https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}'>Таблица</a>"
        )
        send_telegram(msg)
    except Exception as e:
        logging.error(f"Ошибка отчёта: {e}")
        send_telegram(f"⚠️ Ошибка отчёта: {e}")
    # Слот 5 forward — бумажный мониторинг. Своя обработка ошибок внутри,
    # запускается всегда после отчёта (даже если отчёт упал).
    slot5_forward_check()
    # Агент News — дайджест значимых новостей (только если есть важное).
    news_digest()

# ─── TELEGRAM ───────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        logging.info("Отправляю в Telegram...")
        r = requests.post(url, json=payload, timeout=10)
        logging.info(f"Telegram статус: {r.status_code}")
    except Exception as e:
        logging.error(f"Telegram ошибка: {e}")

# ─── АНАЛИЗ ПРИЧИНЫ SKIP (strategy-aware) ───────────────────────────────────
def analyze_skip_reason(signal, votes):
    cfg = get_strategy_config(signal.get("strategy", "v13"))
    reasons = []

    if cfg["uses_tp_rr"] and cfg["uses_rsi"]:
        # v13-style анализ
        try:
            rsi = float(signal.get("rsi", 50))
            rr  = float(signal.get("rr", 0))
            if rsi > 45:
                reasons.append(f"RSI высокий ({rsi:.1f})")
            if rr < 1.5:
                reasons.append(f"R/R низкий ({rr:.1f})")
        except Exception:
            reasons.append("условия не выполнены")
    else:
        # donchian-style анализ (нет TP/RSI)
        try:
            price = float(signal.get("price", 0))
            sl    = float(signal.get("sl", 0))
            if price > 0 and sl > 0:
                sl_pct = abs(price - sl) / price * 100
                if sl_pct > 15:
                    reasons.append(f"SL далеко ({sl_pct:.1f}%)")
        except Exception:
            pass

    skippers = [ai for ai, vote in votes.items() if vote == "SKIP"]
    if skippers:
        reasons.append(f"против: {', '.join(skippers)}")
    return " | ".join(reasons) if reasons else "консилиум отклонил"

# ─── ПРОМПТ (strategy-aware) ────────────────────────────────────────────────
def build_prompt(signal):
    cfg = get_strategy_config(signal.get("strategy", "v13"))

    signal_lines = [
        f"- Symbol: {signal.get('symbol', 'BTC')}",
        f"- Type: {signal.get('type', 'LONG')}",
        f"- Price: {signal.get('price')}",
        f"- Stop Loss: {signal.get('sl')}",
    ]
    if cfg["uses_tp_rr"]:
        signal_lines.append(f"- Take Profit: {signal.get('tp')}")
        signal_lines.append(f"- Risk/Reward: {signal.get('rr')}")
    if cfg["uses_rsi"]:
        signal_lines.append(f"- RSI: {signal.get('rsi')}")
    signal_lines.append(f"- Strategy: {cfg['description']}")
    signal_lines.append(f"- Timeframe: {cfg['tf']}")

    signal_block = "\n".join(signal_lines)

    return f"""You are a crypto trading risk manager. Analyze this BTC trading signal and respond with ONLY one word: BUY or SKIP.

Signal:
{signal_block}

Rules: {cfg['ai_rules']}
Answer with one word only: BUY or SKIP"""

# ─── AI ВЫЗОВЫ ───────────────────────────────────────────────────────────────
def ask_claude(signal):
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=50,
            messages=[{"role": "user", "content": build_prompt(signal)}]
        )
        return "BUY" if "BUY" in msg.content[0].text.upper() else "SKIP"
    except Exception as e:
        logging.error(f"Claude ошибка: {e}")
        return "ERROR"

def ask_deepseek(signal):
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": build_prompt(signal)}],
                "max_tokens": 50
            },
            timeout=30
        )
        if r.status_code != 200:
            logging.error(f"DeepSeek HTTP {r.status_code}: {r.text[:200]}")
            return "ERROR"
        answer = r.json()["choices"][0]["message"]["content"].upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"DeepSeek ошибка: {e}")
        return "ERROR"

def ask_gpt(signal):
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": build_prompt(signal)}],
                "max_tokens": 50
            },
            timeout=30
        )
        if r.status_code != 200:
            logging.error(f"GPT HTTP {r.status_code}: {r.text[:200]}")
            return "ERROR"
        answer = r.json()["choices"][0]["message"]["content"].upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"GPT ошибка: {e}")
        return "ERROR"

def ask_gemini(signal):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": build_prompt(signal)}]}],
            "generationConfig": {
                "maxOutputTokens": 50,
                "thinkingConfig": {"thinkingBudget": 0}
            }
        }
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            logging.error(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
            return "ERROR"
        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            logging.error(f"Gemini нет candidates: {data}")
            return "ERROR"
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        answer = ""
        for part in parts:
            if "text" in part:
                answer = part["text"]
        if not answer:
            logging.error(f"Gemini нет текста: {candidates[0]}")
            return "ERROR"
        return "BUY" if "BUY" in answer.upper() else "SKIP"
    except Exception as e:
        logging.error(f"Gemini ошибка: {e}")
        return "ERROR"

# ─── КОНСИЛИУМ (4 AI) ───────────────────────────────────────────────────────
def run_council(signal):
    cfg = get_strategy_config(signal.get("strategy", "v13"))
    print("=" * 50)
    print(f"СИГНАЛ ({cfg['name']}): {datetime.now().strftime('%d.%m %H:%M')} {signal}")

    claude_vote   = ask_claude(signal)
    deepseek_vote = ask_deepseek(signal)
    gpt_vote      = ask_gpt(signal)
    gemini_vote   = ask_gemini(signal)

    votes = {
        "claude":   claude_vote,
        "deepseek": deepseek_vote,
        "gpt":      gpt_vote,
        "gemini":   gemini_vote
    }

    active_votes = {k: v for k, v in votes.items() if v != "ERROR"}
    error_votes  = {k: v for k, v in votes.items() if v == "ERROR"}
    total_active = len(active_votes)
    buy_count    = sum(1 for v in active_votes.values() if v == "BUY")

    if total_active >= 3:
        majority = (total_active // 2) + 1
        decision = "BUY" if buy_count >= majority else "SKIP"
    else:
        decision = "SKIP"
        logging.warning(f"Мало ответов: {total_active}/4 — автоматический SKIP")

    print(f"Claude:   {claude_vote}")
    print(f"DeepSeek: {deepseek_vote}")
    print(f"GPT:      {gpt_vote}")
    print(f"Gemini:   {gemini_vote}")
    print(f"Решение:  {decision} ({buy_count}/{total_active})")

    skip_reason = ""

    def vote_emoji(v):
        if v == "BUY":   return "🟢 BUY"
        if v == "SKIP":  return "⚪ SKIP"
        return "🔴 ERROR"

    errors_text = ""
    if error_votes:
        errors_text = f"\n⚠️ Ошибки: {', '.join(error_votes.keys())}"

    # ─── Формируем Telegram сообщение в зависимости от стратегии ───────────
    if decision == "BUY":
        if cfg["uses_tp_rr"]:
            # v13-style: с TP, R/R, RSI
            msg = (
                f"🟢 <b>СИГНАЛ: ОТКРЫТЬ СДЕЛКУ</b>\n\n"
                f"💰 {signal.get('symbol')} {signal.get('type')}\n"
                f"📈 Цена: <b>{signal.get('price')}</b>\n"
                f"🛡 SL: {signal.get('sl')}  |  🎯 TP: {signal.get('tp')}\n"
                f"⚖️ R/R: {signal.get('rr')}  |  📊 RSI: {signal.get('rsi')}\n\n"
                f"🤖 Консилиум: <b>{buy_count}/{total_active}</b>\n"
                f"   Claude:   {vote_emoji(claude_vote)}\n"
                f"   DeepSeek: {vote_emoji(deepseek_vote)}\n"
                f"   GPT:      {vote_emoji(gpt_vote)}\n"
                f"   Gemini:   {vote_emoji(gemini_vote)}\n"
                f"📋 Стратегия: {signal.get('strategy', 'v13')} ({cfg['tf']}){errors_text}"
            )
        else:
            # donchian-style: без TP/R/R/RSI, акцент на trend-following
            msg = (
                f"🟢 <b>СИГНАЛ: ОТКРЫТЬ СДЕЛКУ (Breakout)</b>\n\n"
                f"💰 {signal.get('symbol')} {signal.get('type')}\n"
                f"📈 Цена входа: <b>{signal.get('price')}</b>\n"
                f"🛡 SL: {signal.get('sl')} (Donchian exit)\n"
                f"⏱ Таймфрейм: <b>{cfg['tf']}</b>\n"
                f"📋 Тип: Trend-following (Turtle)\n\n"
                f"🤖 Консилиум: <b>{buy_count}/{total_active}</b>\n"
                f"   Claude:   {vote_emoji(claude_vote)}\n"
                f"   DeepSeek: {vote_emoji(deepseek_vote)}\n"
                f"   GPT:      {vote_emoji(gpt_vote)}\n"
                f"   Gemini:   {vote_emoji(gemini_vote)}\n"
                f"📋 Стратегия: {signal.get('strategy')} ({cfg['name']}){errors_text}"
            )
    else:
        skip_reason = analyze_skip_reason(signal, active_votes)
        if cfg["uses_tp_rr"]:
            msg = (
                f"⚪ <b>Сигнал отклонён</b> ({buy_count}/{total_active})\n\n"
                f"💰 {signal.get('symbol')} @ {signal.get('price')}\n"
                f"📊 RSI: {signal.get('rsi')}  |  ⚖️ R/R: {signal.get('rr')}\n\n"
                f"🤖 Claude:   {vote_emoji(claude_vote)}\n"
                f"   DeepSeek: {vote_emoji(deepseek_vote)}\n"
                f"   GPT:      {vote_emoji(gpt_vote)}\n"
                f"   Gemini:   {vote_emoji(gemini_vote)}\n"
                f"❌ Причина: {skip_reason}{errors_text}"
            )
        else:
            msg = (
                f"⚪ <b>Сигнал отклонён</b> ({buy_count}/{total_active}) — {cfg['name']}\n\n"
                f"💰 {signal.get('symbol')} @ {signal.get('price')}\n"
                f"🛡 SL: {signal.get('sl')}  |  ⏱ {cfg['tf']}\n\n"
                f"🤖 Claude:   {vote_emoji(claude_vote)}\n"
                f"   DeepSeek: {vote_emoji(deepseek_vote)}\n"
                f"   GPT:      {vote_emoji(gpt_vote)}\n"
                f"   Gemini:   {vote_emoji(gemini_vote)}\n"
                f"❌ Причина: {skip_reason}{errors_text}"
            )

    send_telegram(msg)
    log_to_sheets(signal, votes, decision, buy_count, total_active, skip_reason)
    return decision, buy_count, total_active, votes

# ─── ROUTES ─────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
        logging.info(f"Webhook получен: {data}")
        signal = {
            "symbol":   data.get("symbol", "BTC"),
            "type":     data.get("type", "LONG"),
            "price":    data.get("price"),
            "sl":       data.get("sl"),
            "tp":       data.get("tp"),
            "rr":       data.get("rr"),
            "rsi":      data.get("rsi"),
            "strategy": data.get("strategy", "v13"),
        }
        decision, buy_count, total, votes = run_council(signal)
        return jsonify({"status": "ok", "decision": decision, "votes": f"{buy_count}/{total}"})
    except Exception as e:
        logging.error(f"Webhook ошибка: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/test", methods=["GET"])
def test():
    signal = {
        "symbol": "BTC", "type": "LONG",
        "price": 82000, "sl": 80500, "tp": 85000,
        "rr": 2.0, "rsi": 32, "strategy": "v13"
    }
    decision, buy_count, total, votes = run_council(signal)
    return jsonify({
        "status": "test fired", "decision": decision,
        "votes": f"{buy_count}/{total}", "details": votes
    })

@app.route("/test_donchian", methods=["GET"])
def test_donchian():
    """Тестовый эндпоинт для проверки Donchian Daily — пинай через браузер."""
    signal = {
        "symbol": "BTC", "type": "LONG",
        "price": 82000, "sl": 75000,
        "strategy": "donchian_daily"
    }
    decision, buy_count, total, votes = run_council(signal)
    return jsonify({
        "status": "donchian test fired", "decision": decision,
        "votes": f"{buy_count}/{total}", "details": votes
    })

@app.route("/tg_test", methods=["GET"])
def tg_test():
    send_telegram("✅ Telegram тест — бот работает\n🤖 4 AI: Claude, DeepSeek, GPT, Gemini")
    return jsonify({"status": "telegram test sent"})

@app.route("/report", methods=["GET"])
def report():
    weekly_report()
    return jsonify({"status": "report sent"})

@app.route("/slot5_check", methods=["GET"])
def slot5_check_route():
    """Ручной запуск forward-проверки Слота 5 (для теста, без денег)."""
    slot5_forward_check()
    return jsonify({"status": "slot5 forward check done"})

@app.route("/news_check", methods=["GET"])
def news_check_route():
    """Ручной запуск агента News (для теста)."""
    news_digest()
    return jsonify({"status": "news digest checked"})

# ─── ДИАГНОСТИКА BYBIT DATA API (для варианта D) ────────────────────────────
@app.route("/bybit_check", methods=["GET"])
def bybit_check():
    """Проверка доступности публичного Bybit kline с Railway IP.
    Не требует ключей. Открыть в браузере, посмотреть JSON-ответ."""
    out = {}
    for sym in ["BTCUSDT", "BNBUSDT", "XRPUSDT"]:
        try:
            r = requests.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "spot", "symbol": sym,
                        "interval": "D", "limit": 5},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15
            )
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:300]}
            lst = body.get("result", {}).get("list", []) if isinstance(body, dict) else []
            out[sym] = {
                "http_status": r.status_code,
                "retCode": body.get("retCode") if isinstance(body, dict) else None,
                "retMsg": body.get("retMsg") if isinstance(body, dict) else None,
                "candles_returned": len(lst),
                "sample": lst[0][:6] if lst else None
            }
        except Exception as e:
            out[sym] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return jsonify({
        "status": "bybit data api check",
        "note": "Если candles_returned > 0 и http_status 200 — вариант D жизнеспособен с Railway",
        "results": out
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sheets": "configured" if GS_JSON else "NOT configured",
        "strategies": list(STRATEGY_CONFIGS.keys()),
        "ai_keys": {
            "claude":   "ok" if ANTHROPIC_API_KEY else "MISSING",
            "deepseek": "ok" if DEEPSEEK_API_KEY else "MISSING",
            "gpt":      "ok" if OPENAI_API_KEY else "MISSING",
            "gemini":   "ok" if GEMINI_API_KEY else "MISSING"
        }
    })

# ─── COUNCIL V15 (консультация по стратегии) ────────────────────────────────
@app.route("/council_v15", methods=["GET"])
def council_v15():
    STRATEGY_PROMPT = """Ты — эксперт по криптотрейдингу и алгоритмическим стратегиям. 

КОНТЕКСТ:
У меня работает стратегия v13 на BTC/USDT 1H:
- Mean Reversion на Bollinger Bands(20, 2.0)
- Вход в пределах 2% от нижней BB
- RSI(14) < 42 ИЛИ Stochastic разворот < 35
- EMA200 макро фильтр (только лонги в бычьем тренде)
- ATR(14) × 1.5 стоп-лосс, TP = верхняя BB
- Результаты: 71 сделка за 1.4 года, WR 39%, PF 1.39, Max DD 5.39% при 50% депо

ЗАДАЧА:
Разработать v15 — ДОПОЛНИТЕЛЬНУЮ стратегию. Цель: суммарно 7-9 сделок/мес.

ПРЕДЛАГАЕМАЯ КОНЦЕПЦИЯ V15 (EMA Pullback на BTC 4H):
1. Макро тренд: close > EMA200
2. Локальный тренд: EMA21 > EMA200
3. Триггер: цена в зоне EMA21 ±1.5%
4. RSI(14): 35-55 (охлаждение)
5. Объём > SMA(20) × 1.2
6. Бычья свеча ИЛИ молот/поглощение
7. Фильтр волатильности: ATR > 0.5% от цены
8. SL: ATR × 1.5, TP: R/R = 2.0
9. Trailing stop на безубыток после +1R

ОТВЕТЬ НА 5 ВОПРОСОВ:
1. ОЦЕНКА (1-10) с обоснованием.
2. ПЕРЕСЕЧЕНИЕ С V13?
3. СЛАБЫЕ МЕСТА?
4. 2-3 УЛУЧШЕНИЯ.
5. АЛЬТЕРНАТИВА если концепция плохая.

Коротко и по делу."""

    results = {}

    # Claude
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": STRATEGY_PROMPT}]
        )
        results["claude"] = msg.content[0].text
    except Exception as e:
        results["claude"] = f"ERROR: {e}"

    # DeepSeek
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "user", "content": STRATEGY_PROMPT}],
                  "max_tokens": 1500, "temperature": 0.7},
            timeout=90
        )
        if r.status_code == 200:
            results["deepseek"] = r.json()["choices"][0]["message"]["content"]
        else:
            results["deepseek"] = f"ERROR HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        results["deepseek"] = f"ERROR: {e}"

    # GPT
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": STRATEGY_PROMPT}],
                  "max_tokens": 1500, "temperature": 0.7},
            timeout=90
        )
        if r.status_code == 200:
            results["gpt"] = r.json()["choices"][0]["message"]["content"]
        else:
            results["gpt"] = f"ERROR HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        results["gpt"] = f"ERROR: {e}"

    # Gemini
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": STRATEGY_PROMPT}]}],
            "generationConfig": {
                "maxOutputTokens": 1500,
                "thinkingConfig": {"thinkingBudget": 0}
            }
        }
        r = requests.post(url, json=payload, timeout=90)
        if r.status_code == 200:
            data = r.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            answer = ""
            for part in parts:
                if "text" in part:
                    answer = part["text"]
            results["gemini"] = answer if answer else "ERROR: нет текста"
        else:
            results["gemini"] = f"ERROR HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        results["gemini"] = f"ERROR: {e}"

    # Отправить в Telegram
    for ai_name, answer in results.items():
        emoji = {"claude": "🟣", "deepseek": "🔵", "gpt": "🟢", "gemini": "🟡"}
        short = answer[:3500] if not answer.startswith("ERROR") else answer
        # Убираем символы которые ломают HTML парсинг Telegram
        safe = short.replace("<", "‹").replace(">", "›").replace("&", "&amp;")
        msg = f"{emoji.get(ai_name, '⚪')} <b>{ai_name.upper()}</b> — v15 анализ\n{safe}"
        send_telegram(msg)

    send_telegram("✅ Консилиум v15 завершён.\nВсе 4 мнения выше. Анализируй и присылай мне в чат.")

    return jsonify({"status": "council complete", "results": {k: v[:200] for k, v in results.items()}})

# ─── СЛОТ 5: FORWARD-МОНИТОРИНГ (бумажный, без денег) ───────────────────────
# Параметры ЗАМОРОЖЕНЫ 17.05.2026 (PROJECT_1_SLOT5_FORWARD_PROCEDURE.md).
# Изменение любой константы = форвард обнуляется. НЕ править без решения.
SLOT5 = {
    "capital": 40000,
    "z_start": 1.52,   # срез 20%   (заморожено)
    "z_main":  2.28,   # срез 30%   (заморожено)
    "z_full":  3.05,   # выход 100% (заморожено)
    "mvrv_buy_thr": 1.0,
    "fg_capit": 20,
    "sp_mom_days": 20,
    "confluence_min": 3,
    # тиры: имя -> (hi, lo, вес доли depo). dd = price/ATH. D: x<=hi.
    "tiers": {"A": (0.45, 0.38, 0.20), "B": (0.38, 0.32, 0.30),
              "C": (0.32, 0.26, 0.30), "D": (0.26, 0.00, 0.15)},
}

def _slot5_tier(dd):
    for nm, (hi, lo, w) in SLOT5["tiers"].items():
        if (lo < dd <= hi) if nm != "D" else (dd <= hi):
            return nm
    return None

# ATH цикла зафиксирован в Пункте 1 (PROJECT_1). Обновляется только если
# текущая цена его превысит. История цены НЕ нужна — для тира достаточно price/ATH.
SLOT5_ATH = 126198

def _get_btc_price():
    """Текущая цена BTC. CoinGecko simple/price (бесплатно, без ключа, не history).
    Fallback на Coinbase spot если CoinGecko недоступен."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                          params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=8)
        r.raise_for_status()
        return float(r.json()["bitcoin"]["usd"])
    except Exception as e:
        logging.warning(f"CoinGecko price fail ({e}), пробую Coinbase")
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=8)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])

def _get_mvrv_history():
    """CoinMetrics community CapMVRVCur (без ключа). -> (mvrv_list, current_mvrv)."""
    url = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    vals, pt = [], None
    # page_size=10000, истории ~4500 точек -> 1 страница. Потолок 3 = запас,
    # НЕ 60 (десятки запросов = WORKER TIMEOUT на Railway).
    for _ in range(3):
        p = {"assets": "btc", "metrics": "CapMVRVCur", "start_time": "2014-01-01",
             "end_time": datetime.now().strftime("%Y-%m-%d"),
             "page_size": 10000, "frequency": "1d"}
        if pt:
            p["next_page_token"] = pt
        r = requests.get(url, params=p, timeout=10)
        r.raise_for_status()
        j = r.json()
        for row in j.get("data", []):
            vals.append(float(row["CapMVRVCur"]))
        pt = j.get("next_page_token")
        if not pt:
            break
    if len(vals) < 500:
        raise RuntimeError(f"MVRV мало точек: {len(vals)}")
    return vals, vals[-1]

def _mvrv_z_now(mvrv_list):
    """Cumulative Z (формула Glassnode, ddof=1) на последней точке = весь список."""
    m = statistics.fmean(mvrv_list)
    s = statistics.stdev(mvrv_list)
    if s == 0:
        return 0.0
    return (mvrv_list[-1] - m) / s

def _sp_closes_stooq():
    """S&P daily closes из Stooq CSV. Колонки: Date,Open,High,Low,Close,Volume."""
    r = requests.get("https://stooq.com/q/d/l/",
                      params={"s": "^spx", "i": "d"}, timeout=6)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        raise RuntimeError(f"Stooq формат неожиданный: {lines[:1]}")
    closes = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) >= 5 and parts[4] not in ("", "N/D"):
            try:
                closes.append(float(parts[4]))
            except ValueError:
                continue
    return closes

def _sp_closes_yahoo():
    """Fallback: Yahoo Finance chart API (без ключа), ~3 мес дневных close."""
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
                      params={"range": "3mo", "interval": "1d"},
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    quote = res["indicators"]["quote"][0]["close"]
    return [c for c in quote if c is not None]

def _get_sp_macro_ok():
    """S&P 20d momentum >= 0. Stooq основной, Yahoo fallback. -> (bool|None, note)."""
    n = SLOT5["sp_mom_days"]
    for name, fn in (("Stooq", _sp_closes_stooq), ("Yahoo", _sp_closes_yahoo)):
        try:
            closes = fn()
            if len(closes) < n + 1:
                logging.warning(f"S&P {name}: мало данных ({len(closes)}), пробую дальше")
                continue
            mom = closes[-1] / closes[-1 - n] - 1.0
            return (mom >= 0), f"S&P 20d {mom*100:+.1f}% ({name})"
        except Exception as e:
            logging.warning(f"S&P {name} fail: {type(e).__name__} {e}")
            continue
    return None, "S&P недоступен (Stooq+Yahoo)"

def _get_fng_now():
    try:
        j = requests.get("https://api.alternative.me/fng/?limit=1&format=json", timeout=6).json()
        return int(j["data"][0]["value"])
    except Exception:
        return None

def _slot5_ws():
    """Лист Slot5_Forward (состояние форварда — Railway эфемерный, состояние в Sheets)."""
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        ss = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
        try:
            ws = ss.worksheet("Slot5_Forward")
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title="Slot5_Forward", rows=2000, cols=14)
            ws.append_row(["Дата", "Цена", "DD%", "MVRV", "Z", "Тир",
                           "Cheap", "MacroOK", "F&G", "Конфл", "Действие",
                           "Сумма$", "Заметка"])
        return ws
    except Exception as e:
        logging.error(f"Slot5_Forward лист: {e}")
        return None

def slot5_forward_check():
    """Еженедельная бумажная проверка Слота 5. НЕ торгует — логирует + Telegram."""
    logging.info("Слот5 forward-проверка...")
    try:
        price = _get_btc_price()
        mvrv_list, mvrv = _get_mvrv_history()
        z = _mvrv_z_now(mvrv_list)
        ath = max(SLOT5_ATH, price)   # ATH-константа, растёт если новый максимум
        dd = price / ath
        tier = _slot5_tier(dd)
        cheap = mvrv <= SLOT5["mvrv_buy_thr"]
        macro_ok, macro_note = _get_sp_macro_ok()
        fng = _get_fng_now()

        # Конфлюэнция: tier активен + cheap + macro_ok + капитуляция (F&G<=20)
        sigs = [tier is not None, bool(cheap), macro_ok is True]
        if fng is not None:
            sigs.append(fng <= SLOT5["fg_capit"])
        confl = sum(1 for x in sigs if x)
        avail = len(sigs)

        ws = _slot5_ws()
        filled = set()
        if ws is not None:
            try:
                for row in ws.get_all_values()[1:]:
                    if len(row) >= 11 and row[10].startswith("BUY"):
                        filled.add(row[5])           # тир из колонки Тир
                    if len(row) >= 11 and row[10].startswith("SELL"):
                        filled.add(row[10])          # метка SELL-уровня
            except Exception as e:
                logging.error(f"Slot5 чтение состояния: {e}")

        # Решение (бумажное)
        action, amount, note = "WAIT", "", macro_note
        if tier and tier not in filled and confl >= SLOT5["confluence_min"]:
            w = SLOT5["tiers"][tier][2]
            amount = round(SLOT5["capital"] * w)
            action = f"BUY {tier}"
            note = f"БУМАЖНЫЙ вход тир {tier} ({w*100:.0f}% депо)"
        elif z >= SLOT5["z_full"] and "SELL_FULL" not in filled:
            action, note = "SELL FULL", f"Z {z:.2f} >= {SLOT5['z_full']} — полный выход"
        elif z >= SLOT5["z_main"] and "SELL_MAIN" not in filled:
            action, note = "SELL MAIN", f"Z {z:.2f} >= {SLOT5['z_main']} — срез 30%"
        elif z >= SLOT5["z_start"] and "SELL_START" not in filled:
            action, note = "SELL START", f"Z {z:.2f} >= {SLOT5['z_start']} — срез 20%"

        today = datetime.now().strftime("%Y-%m-%d")
        if ws is not None:
            try:
                ws.append_row([today, round(price), f"{(dd-1)*100:.1f}",
                               round(mvrv, 3), round(z, 3), tier or "—",
                               "да" if cheap else "нет",
                               "да" if macro_ok else ("нет" if macro_ok is False else "?"),
                               fng if fng is not None else "—",
                               f"{confl}/{avail}", action, amount, note])
            except Exception as e:
                logging.error(f"Slot5 запись: {e}")

        flag = "🟢" if action.startswith("BUY") else ("🔴" if action.startswith("SELL") else "⚪")
        msg = (
            f"{flag} <b>Слот 5 — forward (бумажный)</b>\n"
            f"📅 {today}\n\n"
            f"💰 BTC: <b>${round(price):,}</b>  (от ATH {(dd-1)*100:.1f}%)\n"
            f"📊 MVRV: {mvrv:.2f}  |  MVRV-Z: <b>{z:.2f}</b>\n"
            f"🎯 Тир: {tier or 'нет (ещё дорого)'}\n"
            f"🧩 Конфлюэнция: <b>{confl}/{avail}</b> "
            f"(cheap:{'✓' if cheap else '✗'} macro:{'✓' if macro_ok else '✗'} "
            f"F&G:{fng if fng is not None else '—'})\n"
            f"⚖️ Пороги выхода (заморожены): {SLOT5['z_start']}/{SLOT5['z_main']}/{SLOT5['z_full']}\n\n"
            f"➡️ <b>{action}</b>"
            + (f"  ${amount:,}" if amount else "") + f"\n{note}\n\n"
            f"<i>Бумажный форвард. Реальные деньги заблокированы Пунктом 0.</i>"
        )
        send_telegram(msg)
    except Exception as e:
        logging.error(f"Слот5 forward ошибка: {e}")
        send_telegram(f"⚠️ Слот5 forward ошибка: {e}")

# ─── АГЕНТ NEWS ─────────────────────────────────────────────────────────────
# Роль: КОНТЕКСТ, не сигнал. Дайджест НЕ основание для сделки (ТЗ раздел 4).
# Источник: бесплатные RSS (без ключей/квот). Парсинг штатным xml.etree.
# Фильтр значимости — детерминированный по ключевым словам, НЕ AI.
# Нет значимых новостей за период -> дайджест НЕ отправляется (тишина=нет важного).

NEWS_FEEDS = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Investing":     "https://www.investing.com/rss/news_25.rss",
    "CNBC Econ":     "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
}

# Триггеры значимости (правь под себя). Регистронезависимо.
NEWS_TRIGGERS = [
    "etf", "halving", "sec", "lawsuit", "rate cut", "rate hike", "fed",
    "fomc", "inflation", "cpi", "recession", "war", "sanctions", "hack",
    "exploit", "regulation", "regulator", "ban", "approve", "approval",
    "crash", "liquidation", "bankrupt", "bankruptcy", "default", "tariff",
    "blackrock", "microstrategy", "interest rate", "interest rates",
    "powell", "treasury", "etf inflow", "etf outflow", "halt", "default",
]

def _parse_rss(url, limit=15):
    """RSS -> [(title, link)]. Штатный xml.etree, без зависимостей."""
    import xml.etree.ElementTree as ET
    r = requests.get(url, timeout=10,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        l = (it.findtext("link") or "").strip()
        if t:
            items.append((t, l))
        if len(items) >= limit:
            break
    return items

def news_digest():
    """Значимые новости из RSS. Шлёт дайджест ТОЛЬКО если есть важное."""
    logging.info("Агент News: сбор...")
    try:
        hits = []
        for name, url in NEWS_FEEDS.items():
            try:
                for title, link in _parse_rss(url):
                    low = title.lower()
                    # Поиск по ГРАНИЦАМ СЛОВА: 'war' не ловит 'Warby/Warren',
                    # 'etf' не ловит 'Netflix'. Многословные триггеры — как есть.
                    m = []
                    for kw in NEWS_TRIGGERS:
                        if " " in kw:
                            if kw in low:
                                m.append(kw)
                        elif re.search(r"\b" + re.escape(kw) + r"\b", low):
                            m.append(kw)
                    if m:
                        hits.append((name, title, link, m[0]))
            except Exception as e:
                logging.warning(f"News {name} fail: {type(e).__name__}: {str(e)[:80]}")
                continue

        if not hits:
            logging.info("Агент News: значимого нет — дайджест не отправлен")
            return

        seen, uniq = set(), []
        for h in hits:
            k = h[1][:80].lower()
            if k not in seen:
                seen.add(k)
                uniq.append(h)
        uniq = uniq[:12]

        lines = [f"📰 <b>News дайджест</b> ({datetime.now().strftime('%Y-%m-%d')})",
                 f"Значимых: <b>{len(uniq)}</b>\n"]
        for name, title, link, kw in uniq:
            safe = title.replace("<", "‹").replace(">", "›").replace("&", "&amp;")
            lines.append(f"• [{name}] {safe}\n  🔑<i>{kw}</i>")
        lines.append("\n⚠️ КОНТЕКСТ, не сигнал. НЕ основание для сделки.")
        send_telegram("\n".join(lines))

        try:
            creds = Credentials.from_service_account_info(
                json.loads(GS_JSON),
                scopes=["https://www.googleapis.com/auth/spreadsheets",
                        "https://www.googleapis.com/auth/drive"])
            ss = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
            try:
                nws = ss.worksheet("News")
            except gspread.WorksheetNotFound:
                nws = ss.add_worksheet(title="News", rows=2000, cols=5)
                nws.append_row(["Дата", "Источник", "Заголовок", "Триггер", "Ссылка"])
            today = datetime.now().strftime("%Y-%m-%d")
            for name, title, link, kw in uniq:
                nws.append_row([today, name, title[:300], kw, link[:300]])
        except Exception as e:
            logging.warning(f"News Sheets log fail (не критично): {type(e).__name__}")
    except Exception as e:
        logging.error(f"Агент News ошибка: {e}")
        send_telegram(f"⚠️ Агент News ошибка: {e}")

# ─── SCHEDULER ──────────────────────────────────────────────────────────────
# replace_existing=True + id предотвращают двойную регистрацию в одном процессе.
# Если Railway запускает 2 воркера — добавь в start-команду: gunicorn --workers 1 app:app
scheduler = BackgroundScheduler()
scheduler.add_job(weekly_report, "cron", day_of_week="mon", hour=9, minute=0,
                  id="weekly_report", replace_existing=True)
scheduler.start()

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
