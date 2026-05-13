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
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
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

def log_to_sheets(signal, votes, decision, buy_count, skip_reason=""):
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
        total      = len(week_rows)
        buy_count  = sum(1 for r in week_rows if r.get("Решение") == "BUY")
        skip_count = total - buy_count
        reason_counts = {}
        for r in week_rows:
            reason = r.get("Причина SKIP", "")
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        reason_text = "\n".join([f"   • {r}: {c}x" for r, c in top_reasons]) if top_reasons else "   нет данных"
        strategies = {}
        for r in week_rows:
            s = r.get("Стратегия", "v13")
            strategies[s] = strategies.get(s, 0) + 1
        strat_text = "\n".join([f"   {k}: {v}" for k, v in strategies.items()])
        msg = (
            f"📊 <b>Еженедельный отчёт</b>\n"
            f"📅 {week_ago} → {datetime.now().strftime('%Y-%m-%d')}\n\n"
            f"📡 Всего сигналов: <b>{total}</b>\n"
            f"🟢 BUY: <b>{buy_count}</b>  |  ⚪ SKIP: <b>{skip_count}</b>\n\n"
            f"📈 По стратегиям:\n{strat_text}\n\n"
            f"❌ Топ SKIP причины:\n{reason_text}\n\n"
            f"🔗 <a href='https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}'>Таблица</a>"
        )
        send_telegram(msg)
    except Exception as e:
        logging.error(f"Ошибка отчёта: {e}")
        send_telegram(f"⚠️ Ошибка отчёта: {e}")

# ─── TELEGRAM ───────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram ограничение — 4096 символов, разбиваем если нужно
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=10)
            logging.info(f"Telegram: {r.status_code}")
        except Exception as e:
            logging.error(f"Telegram ошибка: {e}")

# ─── АНАЛИЗ ПРИЧИНЫ SKIP ────────────────────────────────────────────────────
def analyze_skip_reason(signal, votes):
    reasons = []
    try:
        rsi = float(signal.get("rsi", 50))
        rr  = float(signal.get("rr", 0))
        if rsi > 45:
            reasons.append(f"RSI высокий ({rsi:.1f})")
        if rr < 1.5:
            reasons.append(f"R/R низкий ({rr:.1f})")
        skippers = [ai for ai, v in votes.items() if v == "SKIP"]
        if len(skippers) == 3:
            reasons.append("все AI против")
        elif skippers:
            reasons.append(f"против: {', '.join(skippers)}")
    except Exception:
        reasons.append("условия не выполнены")
    return " | ".join(reasons) if reasons else "консилиум отклонил"

# ─── AI ФУНКЦИИ (ТОРГОВЛЯ — 3 модели) ────────────────────────────────────────
def build_prompt(signal):
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
        logging.error(f"Claude: {e}")
        return "SKIP"

def ask_deepseek(signal):
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "user", "content": build_prompt(signal)}],
                  "max_tokens": 50},
            timeout=30
        )
        return "BUY" if "BUY" in r.json()["choices"][0]["message"]["content"].upper() else "SKIP"
    except Exception as e:
        logging.error(f"DeepSeek: {e}")
        return "SKIP"

def ask_gpt(signal):
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": build_prompt(signal)}],
                  "max_tokens": 50},
            timeout=30
        )
        return "BUY" if "BUY" in r.json()["choices"][0]["message"]["content"].upper() else "SKIP"
    except Exception as e:
        logging.error(f"GPT: {e}")
        return "SKIP"

# ─── ТОРГОВЫЙ КОНСИЛИУМ (3 AI) ──────────────────────────────────────────────
def run_council(signal):
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

# ═══════════════════════════════════════════════════════════════════════════
# СТРАТЕГИЧЕСКИЙ КОНСИЛИУМ (4 AI — включая Gemini)
# Для разработки и обсуждения новых стратегий
# ═══════════════════════════════════════════════════════════════════════════

V15_PROMPT = """Ты — эксперт по криптотрейдингу и алгоритмическим стратегиям.

КОНТЕКСТ:
У меня уже работает стратегия v13 на BTC/USDT 1H:
- Mean Reversion на Bollinger Bands(20, 2.0)
- Вход в пределах 2% от нижней BB
- RSI(14) < 42 ИЛИ Stochastic разворот < 35
- EMA200 макро фильтр (только лонги в бычьем тренде)
- ATR(14) × 1.5 стоп-лосс, TP = верхняя BB
- Результаты: 71 сделка за 1.4 года, WR 39%, PF 1.39, Max DD 5.39% при 50% депо
- Edge есть, но мало сделок (~4-5/мес)

ЗАДАЧА:
Хочу добавить v15 — ДОПОЛНИТЕЛЬНУЮ стратегию которая не дублирует v13, а ловит другие движения BTC. Цель: суммарно 7-9 сделок в месяц.

ПРЕДЛАГАЕМАЯ КОНЦЕПЦИЯ V15 (EMA Pullback на BTC 4H):
1. Макро тренд: close > EMA200
2. Локальный тренд: EMA21 > EMA200
3. Триггер: цена в зоне EMA21 ±1.5%
4. RSI(14): 38-55 (охлаждение, НЕ перепроданность как в v13)
5. Объём > SMA(20) × 1.2
6. Бычья свеча (close > open)
7. SL: ATR(14) × 1.5, TP: R/R = 2.0 (фиксированный)

ОТВЕТЬ ЧЁТКО НА 5 ВОПРОСОВ:

1. ОЦЕНКА КОНЦЕПЦИИ (1-10): обоснуй коротко.
2. ПЕРЕСЕЧЕНИЕ С V13: насколько v15 будет дублировать сигналы v13?
3. СЛАБЫЕ МЕСТА: какие условия могут не сработать на BTC 4H?
4. УЛУЧШЕНИЯ: 2-3 конкретных улучшения (параметры, фильтры, логика).
5. АЛЬТЕРНАТИВА: если моя концепция плохая — предложи ЛУЧШУЮ стратегию для дополнения v13.

Отвечай структурированно, на русском языке, без воды."""

def council_ask_claude(prompt):
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        return f"❌ Claude ошибка: {e}"

def council_ask_deepseek(prompt):
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 2000, "temperature": 0.7},
            timeout=120
        )
        if r.status_code != 200:
            return f"❌ DeepSeek HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ DeepSeek ошибка: {e}"

def council_ask_gpt(prompt):
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 2000, "temperature": 0.7},
            timeout=120
        )
        if r.status_code != 200:
            return f"❌ GPT HTTP {r.status_code}: {r.text[:200]}"
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ GPT ошибка: {e}"

def council_ask_gemini(prompt):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.7}},
            timeout=120
        )
        if r.status_code != 200:
            return f"❌ Gemini HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"❌ Gemini ошибка: {e}"

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
    weekly_report()
    return jsonify({"status": "report sent"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sheets": "configured" if GS_JSON else "NOT configured",
        "gemini": "configured" if GEMINI_API_KEY else "NOT configured"
    })

@app.route("/council_v15", methods=["GET"])
def council_v15():
    """Стратегический консилиум: 4 AI обсуждают v15."""
    logging.info("Запуск стратегического консилиума v15...")

    results = {}

    logging.info("Спрашиваю Claude...")
    results["claude"] = council_ask_claude(V15_PROMPT)

    logging.info("Спрашиваю DeepSeek...")
    results["deepseek"] = council_ask_deepseek(V15_PROMPT)

    logging.info("Спрашиваю GPT...")
    results["gpt"] = council_ask_gpt(V15_PROMPT)

    logging.info("Спрашиваю Gemini...")
    results["gemini"] = council_ask_gemini(V15_PROMPT)

    # Отправить в Telegram
    for ai_name, answer in results.items():
        icon = {"claude": "🟣", "deepseek": "🔵", "gpt": "🟢", "gemini": "🟡"}
        header = f"{icon.get(ai_name, '⚪')} <b>{ai_name.upper()}</b> — v15 анализ\n\n"
        send_telegram(header + answer[:3900])

    send_telegram("✅ <b>Консилиум v15 завершён.</b>\nВсе 4 мнения выше. Анализируй и присылай мне в чат.")

    return jsonify({
        "status": "council complete",
        "models": list(results.keys()),
        "claude_length":   len(results.get("claude", "")),
        "deepseek_length": len(results.get("deepseek", "")),
        "gpt_length":      len(results.get("gpt", "")),
        "gemini_length":   len(results.get("gemini", ""))
    })

# ─── SCHEDULER ──────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(weekly_report, "cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
