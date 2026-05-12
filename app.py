from flask import Flask, request, jsonify
import os
import sys
import threading
import requests
import anthropic
from datetime import datetime
from openai import OpenAI
import json

# КЛЮЧИ
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DEEPSEEK_KEY  = os.environ.get('DEEPSEEK_API_KEY', '')
OPENAI_KEY    = os.environ.get('OPENAI_API_KEY', '')
TG_TOKEN      = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT       = os.environ.get('TELEGRAM_CHAT_ID', '')

# Принудительный вывод
print("=" * 50, flush=True)
print("ЗАГРУЗКА ПЕРЕМЕННЫХ:", flush=True)
print("ANTHROPIC: {} символов".format(len(ANTHROPIC_KEY)), flush=True)
print("DEEPSEEK:  {} символов".format(len(DEEPSEEK_KEY)), flush=True)
print("OPENAI:    {} символов".format(len(OPENAI_KEY)), flush=True)
print("TG_TOKEN:  {} символов".format(len(TG_TOKEN)), flush=True)
print("TG_CHAT:   {}".format(TG_CHAT), flush=True)
print("=" * 50, flush=True)
sys.stdout.flush()

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("❌ TG_TOKEN или TG_CHAT пустые!", flush=True)
        return False
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TG_TOKEN)
        r = requests.post(url, data={
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        print("📱 Telegram статус: {}".format(r.status_code), flush=True)
        print("📱 Telegram ответ: {}".format(r.text[:200]), flush=True)
        return r.status_code == 200
    except Exception as e:
        print("❌ Telegram ошибка: {}".format(e), flush=True)
        return False

def build_prompt(data):
    return (
        "BTC технический анализ.\n\n"
        "Текущие данные:\n"
        "- Цена: ${}\n"
        "- RSI: {} (ниже 30 = перепродан, выше 70 = перекуплен)\n"
        "- Стоп-лосс: ${}\n"
        "- Тейк-профит: ${}\n"
        "- R/R: {}\n\n"
        "Стратегия mean reversion ищет вход когда:\n"
        "- RSI < 35 (сильно перепродан)\n"
        "- R/R >= 2.0\n"
        "- Цена у нижней BB\n\n"
        "Учитывая ВСЕ данные выше, что делать?\n"
        "Ответь СТРОГО:\n"
        "СИГНАЛ: BUY (если условия выполнены) или SKIP (если нет)\n"
        "ПРИЧИНА: одно предложение"
    ).format(data.get("price", "?"), data.get("rsi", "?"),
             data.get("sl", "?"), data.get("tp", "?"),
             data.get("rr", "?"))

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
    print("\n" + "=" * 50, flush=True)
    print("СИГНАЛ:", now, data, flush=True)
    
    prompt = build_prompt(data)
    
    c_resp = ask_claude(prompt)
    d_resp = ask_deepseek(prompt)
    g_resp = ask_gpt(prompt)
    
    signals = [extract(c_resp), extract(d_resp), extract(g_resp)]
    buy_count = signals.count("BUY")
    
    print("Claude:   {}".format(signals[0]), flush=True)
    print("DeepSeek: {}".format(signals[1]), flush=True)
    print("GPT:      {}".format(signals[2]), flush=True)
    print("BUY: {}/3".format(buy_count), flush=True)
    
    final = "BUY" if buy_count >= 2 else "SKIP"
    consensus = "{}/3 BUY".format(buy_count)
    
    # ВСЕГДА отправляем в Telegram — для отладки
    if final == "BUY":
        emoji = "🟢"
        title = "ПОДТВЕРЖДЁН"
    else:
        emoji = "⚠️"
        title = "Отклонён AI"
    
    msg = (
        "{} <b>{} {} {}</b>\n\n"
        "💰 Цена: ${}\n"
        "🛑 SL: ${}\n"
        "🎯 TP: ${}\n"
        "📊 R/R: {}\n"
        "📈 RSI: {}\n\n"
        "🤖 Консенсус: {}\n"
        "Финал: {}\n"
        "⏰ {}"
    ).format(emoji, data.get("symbol", "BTC"), data.get("type", "LONG"), title,
             data.get("price", ""), data.get("sl", ""), data.get("tp", ""),
             data.get("rr", ""), data.get("rsi", ""), consensus, final, now)
    
    print("📨 Отправляю в Telegram...", flush=True)
    send_telegram(msg)

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
        print("Webhook error:", e, flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/test", methods=["GET"])
def test():
    test_data = {
        "symbol": "BTC", "type": "LONG", "price": 81500,
        "sl": 80500, "tp": 84500, "rr": 3.0, "rsi": 28.5
    }
    threading.Thread(target=process_signal, args=(test_data,)).start()
    return jsonify({"status": "test fired"}), 200

@app.route("/tg_test", methods=["GET"])
def tg_test():
    """Простой тест Telegram напрямую"""
    result = send_telegram("🤖 Прямой тест Telegram из Railway")
    return jsonify({
        "telegram_sent": result,
        "tg_token_length": len(TG_TOKEN),
        "tg_chat_id": TG_CHAT
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("Сервер запускается на порту", port, flush=True)
    send_telegram("🚀 <b>Railway сервер запущен</b>")
    app.run(host="0.0.0.0", port=port)
