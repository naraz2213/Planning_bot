import asyncio
import json
import os
import threading
from datetime import datetime, timedelta
from collections import defaultdict

from google import genai
from google.genai import types as genai_types
from flask import Flask
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ============ FLASK (для Render) ============
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# ============ ИНИЦИАЛИЗАЦИЯ ============
token = os.getenv("BOT_TOKEN")
if not token:
    raise RuntimeError("BOT_TOKEN не задан в Environment")

gemini_key = os.getenv("GEMINI_API_KEY")
if not gemini_key:
    raise RuntimeError("GEMINI_API_KEY не задан в Environment")

# Новый SDK google-genai
client = genai.Client(api_key=gemini_key)
MODEL_NAME = "gemini-2.5-flash"

bot = Bot(token=token)
dp = Dispatcher()

# ============ ХРАНИЛИЩЕ (in-memory) ============
events_store = defaultdict(list)
templates_store = defaultdict(list)

# ============ ПРОМПТЫ ============
PARSE_PROMPT = """Ты — ассистент-планировщик. Пользователь пишет свои задачи, пары, встречи в свободной, разговорной форме.
Разбери текст и верни JSON-массив событий.

Формат каждого события:
{{
  "title": "краткое название",
  "type": "pair" | "meeting" | "task" | "other",
  "start_at": "YYYY-MM-DDTHH:MM:SS" или null,
  "end_at": "YYYY-MM-DDTHH:MM:SS" или null,
  "location": "строка или null"
}}

Правила:
- Сегодня: {today} ({weekday}), текущее время: {now}
- Если дата/день не указан — считай, что это сегодня (если время уже прошло — завтра)
- "завтра", "послезавтра", "в пятницу" — преобразуй в конкретную дату
- Пользователь может описывать последовательность дел: "сначала почта, потом математика, потом домой, в 16:00 к другу".
  Разбей это на отдельные события. Если время начала не указано — выбери разумное время сам,
  исходя из последовательности и длительности ("час там буду", "пара часа два").
- Если время НЕ указано совсем — поставь start_at = null и выбери разумное время сам:
  * пары → с 9:00 до 15:00
  * встречи → 15:00-19:00
  * задачи → 19:00-21:00
  * распределяй по слотам, чтобы не было наложений
- Если указана длительность ("час", "два часа") — посчитай end_at
- Если длительность не указана: пары — 1.5 часа, остальное — 1 час
- Тип определи сам по контексту (математика/физика/программирование — это pair; почта/магазин — task; к другу — meeting)

Верни ТОЛЬКО JSON-массив, без markdown, без пояснений."""


TEMPLATE_PROMPT = """Ты парсишь шаблон учебной недели. Пользователь описывает пары по дням.
Верни JSON-массив:
[{{"weekday": 0-6, "title": "название", "time": "HH:MM", "location": null}}]
weekday: 0=понедельник, 6=воскресенье.
Только JSON, без пояснений."""


# ============ LLM ============
def _call_gemini_json(prompt: str) -> list:
    """Синхронный вызов Gemini с гарантированным JSON-ответом."""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    raw = response.text.strip()
    data = json.loads(raw)
    if isinstance(data, dict):
        # если модель вернула {"events": [...]} — достаём список
        for key in ("events", "items", "result", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else []


async def parse_with_llm(text: str) -> list:
    now = datetime.now()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    prompt = PARSE_PROMPT.format(
        today=now.strftime("%Y-%m-%d"),
        weekday=weekdays[now.weekday()],
        now=now.strftime("%H:%M"),
    ) + "\n\nТекст пользователя:\n" + text
    try:
        return await asyncio.to_thread(_call_gemini_json, prompt)
    except Exception as e:
        print(f"LLM parse error: {type(e).__name__}: {e}")
        return []


async def parse_template_with_llm(text: str) -> list:
    try:
        return await asyncio.to_thread(_call_gemini_json, TEMPLATE_PROMPT + "\n\n" + text)
    except Exception as e:
        print(f"Template parse error: {type(e).__name__}: {e}")
        return []


# ============ ВЫВОД ============
TYPE_EMOJI = {"pair": "📚", "meeting": "🤝", "task": "✅", "other": "📌"}


def format_events_for_day(user_id: int, date: datetime) -> str:
    lines = []
    day_events = []

    for ev in events_store[user_id]:
        if not ev.get("start_at"):
            continue
        try:
            dt = datetime.fromisoformat(ev["start_at"])
        except Exception:
            continue
        if dt.date() == date.date():
            day_events.append((dt, ev))

    for t in templates_store[user_id]:
        if t.get("weekday") == date.weekday():
            day_events.append((None, {
                "title": t["title"],
                "type": "pair",
                "start_at": t.get("time"),
                "location": t.get("location"),
                "_from_template": True,
            }))

    if not day_events:
        return ""

    day_events.sort(key=lambda x: (x[0] is None, x[0] if x[0] else datetime.max))

    header = date.strftime("%d.%m (%a)")
    for dt, ev in day_events:
        emoji = TYPE_EMOJI.get(ev.get("type", "other"), "📌")
        time_str = ""
        if dt:
            time_str = dt.strftime("%H:%M")
        elif isinstance(ev.get("start_at"), str):
            s = ev["start_at"]
            time_str = s.split("T")[1][:5] if "T" in s else s

        loc = f" — {ev['location']}" if ev.get("location") else ""
        mark = " 🔁" if ev.get("_from_template") else ""
        lines.append(f"{emoji} {time_str} {ev['title']}{loc}{mark}")

    return f"*{header}*\n" + "\n".join(lines)


def format_range(user_id: int, days: int) -> str:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    blocks = []
    for i in range(days):
        d = today + timedelta(days=i)
        block = format_events_for_day(user_id, d)
        if block:
            blocks.append(block)
    if not blocks:
        return "Пусто. Добавь события текстом или задай шаблон через /template."
    return "\n\n".join(blocks)


# ============ ХЕНДЛЕРЫ ============
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот-планировщик.\n\n"
        "📝 Напиши мне свои задачи/пары/встречи свободным текстом — я разберу и добавлю в расписание.\n"
        "Например: «завтра в 10 пара по матану, в 15 встреча с научруком, вечером доделать отчёт»\n\n"
        "Команды:\n"
        "/today — расписание на сегодня\n"
        "/week — на неделю\n"
        "/template — задать стандартный шаблон недели\n"
        "/show_template — показать шаблон\n"
        "/clear — очистить всё"
    )


@dp.message(Command("today"))
async def cmd_today(message: types.Message):
    await message.answer(format_range(message.from_user.id, 1), parse_mode="Markdown")


@dp.message(Command("week"))
async def cmd_week(message: types.Message):
    await message.answer(format_range(message.from_user.id, 7), parse_mode="Markdown")


@dp.message(Command("show_template"))
async def cmd_show_template(message: types.Message):
    tpl = templates_store[message.from_user.id]
    if not tpl:
        await message.answer("Шаблон пуст. Задай его через /template")
        return
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = ["*Шаблон недели:*"]
    for t in sorted(tpl, key=lambda x: (x.get("weekday", 0), x.get("time", ""))):
        loc = f" — {t['location']}" if t.get("location") else ""
        wd = weekdays[t.get("weekday", 0)]
        lines.append(f"{wd} {t.get('time', '')} {t['title']}{loc}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("template"))
async def cmd_template(message: types.Message):
    await message.answer(
        "Опиши свой стандартный шаблон недели свободным текстом.\n\n"
        "Пример:\n"
        "Понедельник: матан 10:00, физика 12:00, англ 14:00\n"
        "Вторник: программирование 10:00, физра 12:00\n"
        "Среда: матан 10:00\n\n"
        "Отправь одним сообщением, я разберу."
    )


@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    events_store[message.from_user.id].clear()
    templates_store[message.from_user.id].clear()
    await message.answer("Всё очищено.")


@dp.message()
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    text = message.text
    if not text:
        return

    # Шаблон? (если в тексте ≥2 названий дней недели ИЛИ явные строки)
    weekdays_lower = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    wd_count = sum(1 for w in weekdays_lower if w in text.lower())
    is_template = wd_count >= 2 or (wd_count >= 1 and text.count("\n") >= 1)

    if is_template:
        await message.answer("⏳ Разбираю шаблон...")
        items = await parse_template_with_llm(text)
        if items:
            templates_store[user_id] = items
            await message.answer(f"✅ Шаблон сохранён: {len(items)} пар. Проверь /show_template")
            return
        await message.answer("Не смог разобрать шаблон. Попробуй ещё раз.")
        return

    # Обычные события
    await message.answer("⏳ Разбираю...")
    parsed = await parse_with_llm(text)
    if not parsed:
        await message.answer("Не смог разобрать. Попробуй переформулировать.")
        return

    for ev in parsed:
        events_store[user_id].append(ev)

    lines = ["✅ Добавлено:"]
    for ev in parsed:
        emoji = TYPE_EMOJI.get(ev.get("type", "other"), "📌")
        when = ""
        if ev.get("start_at"):
            try:
                dt = datetime.fromisoformat(ev["start_at"])
                when = dt.strftime("%d.%m %H:%M")
            except Exception:
                when = ev["start_at"]
        lines.append(f"{emoji} {when} {ev.get('title', '?')}")
    await message.answer("\n".join(lines))


# ============ ЗАПУСК ============
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    print(f"Bot starting, model={MODEL_NAME}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
