import os
import json
import logging
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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SPREADSHEET_ID     = os.environ.get("SPREADSHEET_ID", "")
GS_JSON            = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

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
                "Claude", "DeepSeek", "GPT",
                "Голоса", "Решение", "Стратегия", "Причина SKIP"
            ]
            sheet.append_row(headers)
    except Exception as e:
        logging.error(f"Ошибка заголовка: {e}")

def log_to_sheets(signal: dict, votes: dict, decision: str, buy_count: int, skip_reason: str = ""):
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
        f"{buy_count}/3",
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
def weekly_report():
    """Каждый понедельник 9:00 UTC — статистика за 7 дней."""
    logging.info("Генерирую еженедельный отчёт...")
    sheet = get_sheet()
    if sheet is None:
        send_telegram("⚠️ Еженедельный отчёт: не могу подключиться к Sheets")
        return
    try:
        all_rows = sheet.get_all_records()
        if not all_rows:
            send_telegram("📊 Еженедельный отчёт: данных пока нет")
            return

        week_ago  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        week_rows = [r for r in all_rows if str(r.get("Дата", "")) >= week_ago]
        total     = len(week_rows)
        buy_count = sum(1 for r in week_rows if r.get("Решение") == "BUY")
        skip_count = total - buy_count

        # Топ причины SKIP
        reason_counts = {}
        for r in week_rows:
            reason = r.get("Причина SKIP", "")
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        reason_text = "\n".join([f"   • {r}: {c}x" for r, c in top_reasons]) if top_reasons else "   нет данных"

        # По стратегиям
        strategies = {}
        for r in week_rows:
            s = r.get("Стратегия", "v13")
            strategies[s] = strategies.get(s, 0) + 1
        strat_text = "\n".join([f"   {k}: {v} сигналов" for k, v in strategies.items()])

        msg = (
            f"📊 <b>Еженедельный отчёт</b>\n"
            f"📅 {week_ago} → {datetime.now().strftime('%Y-%m-%d')}\n\n"
            f"📡 Всего сигналов: <b>{total}</b>\n"
            f"🟢 BUY:  <b>{buy_count}</b>\n"
            f"⚪ SKIP: <b>{skip_count}</b>\n\n"
            f"📈 По стратегиям:\n{strat_text}\n\n"
            f"❌ Топ причины SKIP:\n{reason_text}\n\n"
            f"🔗 <a href='https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}'>Открыть таблицу</a>"
        )
        send_telegram(msg)
        logging.info("Еженедельный отчёт отправлен")

    except Exception as e:
        logging.error(f"Ошибка отчёта: {e}")
        send_telegram(f"⚠️ Ошибка еженедельного отчёта: {e}")

# ─── TELEGRAM ───────────────────────────────────────────────────────────────
def send_telegram(message: str):
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

# ─── АНАЛИЗ ПРИЧИНЫ SKIP ────────────────────────────────────────────────────
def analyze_skip_reason(signal: dict, votes: dict) -> str:
    reasons = []
    try:
        rsi = float(signal.get("rsi", 50))
        rr  = float(signal.get("rr", 0))
        if rsi > 45:
            reasons.append(f"RSI высокий ({rsi:.1f})")
        if rr < 1.5:
            reasons.append(f"R/R низкий ({rr:.1f})")
        skippers = [ai for ai, vote in votes.items() if vote == "SKIP"]
        if len(skippers) == 3:
            reasons.append("все AI против")
        elif skippers:
            reasons.append(f"против: {', '.join(skippers)}")
    except Exception:
        reasons.append("условия не выполнены")
    return " | ".join(reasons) if reasons else "консилиум отклонил"

# ─── AI КОНСИЛИУМ ───────────────────────────────────────────────────────────
def build_prompt(signal: dict) -> str:
    return f"""You are a crypto trading risk manager. Analyze this BTC trading signal and respond with ONLY one word: BUY or SKIP.

Signal:
- Symbol: {signal.get('symbol', 'BTC')}
- Type: {signal.get('type', 'LONG')}
- Price: {signal.get('price')}
- Stop Loss: {signal.get('sl')}
- Take Profit: {signal.get('tp')}
- Risk/Reward: {signal.get('rr')}
- RSI: {signal.get('rsi')}
- Strategy: Mean Reversion on Bollinger Bands lower band, BTC/USDT 1H

Rules: BUY only if R/R >= 1.5 and RSI < 45 and signal is LONG. Otherwise SKIP.
Answer with one word only: BUY or SKIP"""

def ask_claude(signal: dict) -> str:
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
        return "SKIP"

def ask_deepseek(signal: dict) -> str:
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "user", "content": build_prompt(signal)}],
                  "max_tokens": 50},
            timeout=30
        )
        answer = r.json()["choices"][0]["message"]["content"].upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"DeepSeek ошибка: {e}")
        return "SKIP"

def ask_gpt(signal: dict) -> str:
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": build_prompt(signal)}],
                  "max_tokens": 50},
            timeout=30
        )
        answer = r.json()["choices"][0]["message"]["content"].upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"GPT ошибка: {e}")
        return "SKIP"

# ─── КОНСИЛИУМ ──────────────────────────────────────────────────────────────
def run_council(signal: dict):
    print("=" * 50)
    print(f"СИГНАЛ: {datetime.now().strftime('%d.%m %H:%M')} {signal}")

    claude_vote   = ask_claude(signal)
    deepseek_vote = ask_deepseek(signal)
    gpt_vote      = ask_gpt(signal)

    votes     = {"claude": claude_vote, "deepseek": deepseek_vote, "gpt": gpt_vote}
    buy_count = sum(1 for v in votes.values() if v == "BUY")
    decision  = "BUY" if buy_count >= 2 else "SKIP"
    skip_reason = ""

    print(f"Claude: {claude_vote} | DeepSeek: {deepseek_vote} | GPT: {gpt_vote}")
    print(f"Решение: {decision} ({buy_count}/3)")

    if decision == "BUY":
        msg = (
            f"🟢 <b>СИГНАЛ: ОТКРЫТЬ СДЕЛКУ</b>\n\n"
            f"💰 {signal.get('symbol')} {signal.get('type')}\n"
            f"📈 Цена: <b>{signal.get('price')}</b>\n"
            f"🛡 SL: {signal.get('sl')}  |  🎯 TP: {signal.get('tp')}\n"
            f"⚖️ R/R: {signal.get('rr')}  |  📊 RSI: {signal.get('rsi')}\n\n"
            f"🤖 Консилиум: {buy_count}/3\n"
            f"   Claude: {claude_vote} | DeepSeek: {deepseek_vote} | GPT: {gpt_vote}\n"
            f"📋 Стратегия: {signal.get('strategy', 'v13')}"
        )
    else:
        skip_reason = analyze_skip_reason(signal, votes)
        msg = (
            f"⚪ <b>Сигнал отклонён</b> ({buy_count}/3)\n\n"
            f"💰 {signal.get('symbol')} @ {signal.get('price')}\n"
            f"📊 RSI: {signal.get('rsi')}  |  ⚖️ R/R: {signal.get('rr')}\n\n"
            f"🤖 Claude: {claude_vote} | DeepSeek: {deepseek_vote} | GPT: {gpt_vote}\n"
            f"❌ Причина: {skip_reason}"
        )

    send_telegram(msg)
    log_to_sheets(signal, votes, decision, buy_count, skip_reason)
    return decision, buy_count, votes

# ─── ROUTES ─────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
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
        decision, buy_count, votes = run_council(signal)
        return jsonify({"status": "ok", "decision": decision, "votes": f"{buy_count}/3"})
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
    decision, buy_count, votes = run_council(signal)
    return jsonify({"status": "test fired", "decision": decision,
                    "votes": f"{buy_count}/3", "details": votes})

@app.route("/tg_test", methods=["GET"])
def tg_test():
    send_telegram("✅ Telegram тест — бот работает")
    return jsonify({"status": "telegram test sent"})

@app.route("/report", methods=["GET"])
def report():
    """Ручной запуск отчёта в любое время."""
    weekly_report()
    return jsonify({"status": "report sent"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sheets": "configured" if GS_JSON else "NOT configured"
    })

# ─── SCHEDULER (еженедельный отчёт) ─────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(weekly_report, "cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
