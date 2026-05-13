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
def weekly_report():
    logging.info("Генерирую еженедельный отчёт...")
    sheet = get_sheet()
    if sheet is None:
        send_telegram("⚠️ Еженедельный отчёт: Sheets недоступен")
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
        skippers = [ai for ai, vote in votes.items() if vote == "SKIP"]
        if skippers:
            reasons.append(f"против: {', '.join(skippers)}")
    except Exception:
        reasons.append("условия не выполнены")
    return " | ".join(reasons) if reasons else "консилиум отклонил"

# ─── ПРОМПТ ──────────────────────────────────────────────────────────────────
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
            "generationConfig": {"maxOutputTokens": 50}
        }
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            logging.error(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
            return "ERROR"
        answer = r.json()["candidates"][0]["content"]["parts"][0]["text"].upper()
        return "BUY" if "BUY" in answer else "SKIP"
    except Exception as e:
        logging.error(f"Gemini ошибка: {e}")
        return "ERROR"

# ─── КОНСИЛИУМ (4 AI) ───────────────────────────────────────────────────────
def run_council(signal):
    print("=" * 50)
    print(f"СИГНАЛ: {datetime.now().strftime('%d.%m %H:%M')} {signal}")

    # Опрос всех 4 AI
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

    # Считаем только успешные ответы (не ERROR)
    active_votes = {k: v for k, v in votes.items() if v != "ERROR"}
    error_votes  = {k: v for k, v in votes.items() if v == "ERROR"}
    total_active = len(active_votes)
    buy_count    = sum(1 for v in active_votes.values() if v == "BUY")

    # Логика решения:
    # 4 из 4 ответили → нужно 3+ BUY
    # 3 из 4 ответили → нужно 2+ BUY
    # 2 из 4 ответили → нужно 2 BUY
    # 1 или 0 ответили → SKIP (ненадёжно)
    if total_active >= 3:
        majority = (total_active // 2) + 1  # 3→2, 4→3
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

    # Формируем голоса для Telegram
    def vote_emoji(v):
        if v == "BUY":   return "🟢 BUY"
        if v == "SKIP":  return "⚪ SKIP"
        return "🔴 ERROR"

    errors_text = ""
    if error_votes:
        errors_text = f"\n⚠️ Ошибки: {', '.join(error_votes.keys())}"

    if decision == "BUY":
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
            f"📋 Стратегия: {signal.get('strategy', 'v13')}{errors_text}"
        )
    else:
        skip_reason = analyze_skip_reason(signal, active_votes)
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

@app.route("/tg_test", methods=["GET"])
def tg_test():
    send_telegram("✅ Telegram тест — бот работает\n🤖 4 AI: Claude, DeepSeek, GPT, Gemini")
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
    """Консилиум по разработке v15 — все 4 AI дают мнение."""

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
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": STRATEGY_PROMPT}]}],
            "generationConfig": {"maxOutputTokens": 1500}
        }
        r = requests.post(url, json=payload, timeout=90)
        if r.status_code == 200:
            results["gemini"] = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        else:
            results["gemini"] = f"ERROR HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        results["gemini"] = f"ERROR: {e}"

    # Отправить в Telegram
    for ai_name, answer in results.items():
        emoji = {"claude": "🟣", "deepseek": "🔵", "gpt": "🟢", "gemini": "🟡"}
        short = answer[:3500] if not answer.startswith("ERROR") else answer
        msg = f"{emoji.get(ai_name, '⚪')} <b>{ai_name.upper()}</b> — v15 анализ\n{short}"
        send_telegram(msg)

    send_telegram("✅ Консилиум v15 завершён.\nВсе 4 мнения выше. Анализируй и присылай мне в чат.")

    return jsonify({"status": "council complete", "results": {k: v[:200] for k, v in results.items()}})

# ─── SCHEDULER ──────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(weekly_report, "cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
