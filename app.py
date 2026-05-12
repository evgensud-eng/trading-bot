from flask import Flask, request, jsonify
import os
import threading
import requests
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from openai import OpenAI
import google.generativeai as genai
import json

# КЛЮЧИ из переменных окружения
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY')
DEEPSEEK_KEY   = os.environ.get('DEEPSEEK_API_KEY')
OPENAI_KEY     = os.environ.get('OPENAI_API_KEY')
GEMINI_KEY     = os.environ.get('GEMINI_API_KEY')
TG_TOKEN       = os.environ.get('TELEGRAM_BOT_TOKEN')
TG_CHAT        = os.environ.get('TELEGRAM_CHAT_ID')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '1xBxae-SlDvhcxOfCuW3pb5tpeQTxd0sVCx2VRrehExY')
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDS_JSON', '')

# TELEGRAM
def send_telegram(text):
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TG_TOKEN)
        requests.post(url, data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# GOOGLE SHEETS
WS = None
try:
    if GOOGLE_CREDS_JSON:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets',
                  'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        WS = sh.sheet1
        if len(WS.get_all_values()) == 0:
            WS.append_row([
                "Время", "Тип", "Монета", "Цена", "SL", "TP", "R/R",
                "Claude", "DeepSeek", "GPT", "Консенсус", "Финал"
            ])
        print("Google Sheets подключён")
except Exception as e:
    print("Sheets не подключён:", e)

# AI
def build_prompt(data):
    return (
        "Сигнал от стратегии v13 (Mean Reversion):\n\n"
        "Монета: {}\n"
        "Тип: {}\n"
        "Цена: ${}\n"
        "Стоп: ${}\n"
        "Тейк: ${}\n"
        "R/R: {}\n"
        "RSI: {}\n\n"
        "Подтверждаешь?\n"
        "СИГНАЛ: [BUY / SKIP]\n"
        "ПРИЧИНА: одно предложение"
    ).format(data.get("symbol", "BTC"), data.get("type", "LONG"),
             data.get("price", "?"), data.get("sl", "?"),
             data.get("tp", "?"), data.get("rr", "?"),
             data.get("rsi", "?"))

def ask_claude(p):
    try:
        c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        m = c.messages.create(model="claude-sonnet-4-5", max_tokens=150,
                              messages=[{"role":"user","content":p}])
        return m.content[0].text
    except Exception as e:
        return "СИГНАЛ: SKIP\nОшибка: " + str(e)[:50]

def ask_deepseek(p):
    try:
        c = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com", timeout=20)
        r = c.chat.completions.create(model="deepseek-chat", max_tokens=150,
                                      messages=[{"role":"user","content":p}])
        return r.choices[0].message.content
    except Exception as e:
        return "СИГНАЛ: SKIP\nОшибка: " + str(e)[:50]

def ask_gpt(p):
    try:
        c = OpenAI(api_key=OPENAI_KEY, timeout=20)
        r = c.chat.completions.create(model="gpt-4o-mini", max_tokens=150,
                                      messages=[{"role":"user","content":p}])
        return r.choices[0].message.content
    except Exception as e:
        return "СИГНАЛ: SKIP\nОшибка: " + str(e)[:50]

def extract(text):
    return "BUY" if "BUY" in text.upper() else "SKIP"

def process_signal(data):
    now = datetime.now().strftime('%d.%m %H:%M')
    print("СИГНАЛ:", now, data)
    
    prompt = build_prompt(data)
    
    c_resp = ask_claude(prompt)
    d_resp = ask_deepseek(prompt)
    g_resp = ask_gpt(prompt)
    
    signals = [extract(c_resp), extract(d_resp), extract(g_resp)]
    buy_count = signals.count("BUY")
    
    print("Claude:   {}".format(signals[0]))
    print("DeepSeek: {}".format(signals[1]))
    print("GPT:      {}".format(signals[2]))
    print("BUY: {}/3".format(buy_count))
    
    final = "BUY" if buy_count >= 2 else "SKIP"
    consensus = "{}/3 BUY".format(buy_count)
    
    # Sheets
    if WS:
        try:
            WS.append_row([
                now, data.get("type", "LONG"), data.get("symbol", "BTC"),
                str(data.get("price", "")), str(data.get("sl", "")),
                str(data.get("tp", "")), str(data.get("rr", "")),
                signals[0], signals[1], signals[2], consensus, final
            ])
        except Exception as e:
            print("Sheets error:", e)
    
    # Telegram
    if final == "BUY":
        emoji = "🟢" if data.get("type") == "LONG" else "🔴"
        msg = (
            "{} <b>{} {} ПОДТВЕРЖДЁН</b>\n\n"
            "💰 Цена: ${}\n"
            "🛑 SL: ${}\n"
            "🎯 TP: ${}\n"
            "📊 R/R: {}\n"
            "📈 RSI: {}\n\n"
            "🤖 Консенсус: {} ✅\n"
            "⏰ {}"
        ).format(emoji, data.get("symbol", "BTC"), data.get("type", "LONG"),
                 data.get("price", ""), data.get("sl", ""), data.get("tp", ""),
                 data.get("rr", ""), data.get("rsi", ""), consensus, now)
        send_telegram(msg)
    else:
        msg = (
            "⚠️ <b>Сигнал отклонён</b>\n"
            "{} {} ${}\n"
            "Консенсус: {} (нужно 2/3)\n"
            "⏰ {}"
        ).format(data.get("symbol", "BTC"), data.get("type", "LONG"),
                 data.get("price", ""), consensus, now)
        send_telegram(msg)

# FLASK
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Trading Webhook Server v13 — Railway production"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        if not data:
            raw = request.data.decode('utf-8')
            data = {"raw": raw}
        threading.Thread(target=process_signal, args=(data,)).start()
        return jsonify({"status": "received"}), 200
    except Exception as e:
        print("Webhook error:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/test", methods=["GET"])
def test():
    test_data = {
        "symbol": "BTC", "type": "LONG", "price": 81500,
        "sl": 80500, "tp": 84500, "rr": 3.0, "rsi": 28.5
    }
    threading.Thread(target=process_signal, args=(test_data,)).start()
    return jsonify({"status": "test fired"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("Запуск сервера на порту", port)
    send_telegram("🚀 <b>Railway сервер запущен</b>")
    app.run(host="0.0.0.0", port=port)
