import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from datetime import datetime
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
    """Подключение к Google Sheets через Service Account."""
    try:
        creds_dict = json.loads(GS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        return sheet
    except Exception as e:
        logging.error(f"Google Sheets подключение: {e}")
        return None

def ensure_header(sheet):
    """Создать заголовок если таблица пустая."""
    try:
        first_row = sheet.row_values(1)
        if not first_row:
            headers = [
                "Дата", "Время", "Символ", "Тип",
                "Цена", "SL", "TP", "R/R", "RSI",
                "Claude", "DeepSeek", "GPT",
                "Голоса", "Решение", "Стратегия"
            ]
            sheet.append_row(headers)
            logging.info("Заголовок таблицы создан")
    except Exception as e:
        logging.error(f"Ошибка заголовка: {e}")

def log_to_sheets(signal: dict, votes: dict, decision: str, buy_count: int):
    """Записать сигнал и решение в Google Sheets."""
    sheet = get_sheet()
    if sheet is None:
        logging.error("Sheets недоступен — запись пропущена")
        return False

    ensure_header(sheet)

    now = datetime.now()
    row = [
        now.strftime("%Y-%m-%d"),           # Дата
        now.strftime("%H:%M:%S"),           # Время
        signal.get("symbol", "BTC"),        # Символ
        signal.get("type", "LONG"),         # Тип
        signal.get("price", ""),            # Цена
        signal.get("sl", ""),               # SL
        signal.get("tp", ""),               # TP
        signal.get("rr", ""),               # R/R
        signal.get("rsi", ""),              # RSI
        votes.get("claude", "SKIP"),        # Claude
        votes.get("deepseek", "SKIP"),      # DeepSeek
        votes.get("gpt", "SKIP"),           # GPT
        f"{buy_count}/3",                   # Голоса
        decision,                           # BUY / SKIP
        signal.get("strategy", "v13"),      # Стратегия
    ]

    try:
        sheet.append_row(row)
        logging.info(f"Sheets: записано — {decision} {signal.get('symbol')} @ {signal.get('price')}")
        return True
    except Exception as e:
        logging.error(f"Sheets append_row: {e}")
        return False

# ─── TELEGRAM ───────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        logging.info("Отправляю в Telegram...")
        r = requests.post(url, json=payload, timeout=10)
        logging.info(f"Telegram статус: {r.status_code}")
        logging.info(f"Telegram ответ: {r.text[:200]}")
    except Exception as e:
        logging.error(f"Telegram ошибка: {e}")

# ─── AI КОНСИЛИУМ ───────────────────────────────────────────────────────────
def ask_claude(signal: dict) -> str:
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = build_prompt(signal)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}]
        )
        answer = msg.content[0].text.strip().upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"Claude ошибка: {e}")
        return "SKIP"

def ask_deepseek(signal: dict) -> str:
    try:
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": build_prompt(signal)}],
            "max_tokens": 50
        }
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers=headers, json=payload, timeout=30
        )
        answer = r.json()["choices"][0]["message"]["content"].strip().upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"DeepSeek ошибка: {e}")
        return "SKIP"

def ask_gpt(signal: dict) -> str:
    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": build_prompt(signal)}],
            "max_tokens": 50
        }
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers, json=payload, timeout=30
        )
        answer = r.json()["choices"][0]["message"]["content"].strip().upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"GPT ошибка: {e}")
        return "SKIP"

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

# ─── КОНСИЛИУМ ──────────────────────────────────────────────────────────────
def run_council(signal: dict):
    print("=" * 50)
    print(f"СИГНАЛ: {datetime.now().strftime('%d.%m %H:%M')} {signal}")

    claude_vote    = ask_claude(signal)
    deepseek_vote  = ask_deepseek(signal)
    gpt_vote       = ask_gpt(signal)

    votes = {
        "claude":   claude_vote,
        "deepseek": deepseek_vote,
        "gpt":      gpt_vote
    }

    print(f"Claude:   {claude_vote}")
    print(f"DeepSeek: {deepseek_vote}")
    print(f"GPT:      {gpt_vote}")

    buy_count = sum(1 for v in votes.values() if v == "BUY")
    decision  = "BUY" if buy_count >= 2 else "SKIP"
    print(f"BUY: {buy_count}/3")

    # Telegram сообщение
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
        msg = (
            f"⚪ Сигнал отклонён ({buy_count}/3)\n"
            f"{signal.get('symbol')} @ {signal.get('price')} | RSI: {signal.get('rsi')}\n"
            f"Claude: {claude_vote} | DeepSeek: {deepseek_vote} | GPT: {gpt_vote}"
        )

    send_telegram(msg)

    # Google Sheets запись
    sheets_ok = log_to_sheets(signal, votes, decision, buy_count)
    if sheets_ok:
        logging.info("Sheets: ✅ записано")
    else:
        logging.warning("Sheets: ❌ запись не удалась")

    return decision, buy_count, votes

# ─── ROUTES ─────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
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
        "rr": 2.0, "rsi": 32,
        "strategy": "v13"
    }
    decision, buy_count, votes = run_council(signal)
    return jsonify({
        "status": "test fired",
        "decision": decision,
        "votes": f"{buy_count}/3",
        "details": votes
    })

@app.route("/tg_test", methods=["GET"])
def tg_test():
    send_telegram("✅ Telegram тест — бот работает")
    return jsonify({"status": "telegram test sent"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sheets": "configured" if GS_JSON else "NOT configured"
    })

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
