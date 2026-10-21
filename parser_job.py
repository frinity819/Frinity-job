import os
import re
import sqlite3
import random
import datetime
import asyncio
import urllib.parse
import logging
import requests
import time
from playwright.async_api import async_playwright

# ============================================================
# НАСТРОЙКИ (подтягиваются из GitHub Secrets)
# ============================================================
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
DB_PATH = "listings.db"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS listings (link TEXT PRIMARY KEY, title TEXT, first_seen TEXT)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT, price TEXT, 
            price_numeric REAL, views INTEGER, favorites INTEGER, 
            posted_date TEXT, snapshot_at TEXT,
            FOREIGN KEY(link) REFERENCES listings(link)
        )
    """)
    conn.commit()
    return conn

def parse_price_numeric(price_str):
    if not price_str: return None
    m = re.search(r"([\d\.]+)(?:,(\d{2}))?", price_str)
    if not m: return None
    int_part = m.group(1).replace(".", "")
    frac = m.group(2) or "00"
    try: return float(f"{int_part}.{frac}")
    except ValueError: return None

def record_listing(conn, item):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO listings (link, title, first_seen) VALUES (?, ?, ?) ON CONFLICT(link) DO NOTHING",
                 (item["link"], item["title"], now))
    
    last_row = conn.execute("SELECT price FROM snapshots WHERE link=? ORDER BY id DESC LIMIT 1", 
                            (item["link"],)).fetchone()
    last_price = last_row[0] if last_row else None
    
    if last_price != item["price"]:
        conn.execute("""
            INSERT INTO snapshots (link, price, price_numeric, views, favorites, posted_date, snapshot_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (item["link"], item["price"], parse_price_numeric(item["price"]),
              item["views"], item["favorites"], item["posted_date"], now))
        conn.commit()
        return True
    return False

# ============================================================
# TELEGRAM API (ОТПРАВКА И ОЖИДАНИЕ)
# ============================================================
def send_telegram_msg(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        log.error(f"Ошибка отправки сообщения: {e}")

def send_telegram_db():
    if not TG_TOKEN or not TG_CHAT_ID: return
    log.info("Отправка базы данных в Telegram...")
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
        with open(DB_PATH, 'rb') as f:
            files = {'document': ('listings.db', f, 'application/x-sqlite3')}
            data = {'chat_id': TG_CHAT_ID, 'caption': '📁 База данных за текущий запуск'}
            requests.post(url, files=files, data=data, timeout=30)
    except Exception as e:
        log.error(f"Ошибка отправки файла: {e}")

def wait_for_user_url():
    """Ожидает сообщение от пользователя с URL и количеством страниц (2 часа)."""
    log.info("Ожидание ответа от пользователя в Telegram...")
    
    # Сбрасываем старые сообщения, чтобы не схватить вчерашнее
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset=-1", timeout=10)
        updates = r.json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else 0
    except Exception:
        offset = 0

    send_telegram_msg("🤖 <b>Бот запущен!</b>\n\nОтправьте мне ссылку на поиск Kleinanzeigen.\n"
                      "Вы можете добавить количество страниц через пробел.\n"
                      "Пример: <code>https://www.kleinanzeigen.de/s-multimedia-elektronik/... 3</code>\n\n"
                      "⏳ У вас есть 2 часа.")

    start_time = time.time()
    # Ждем 2 часа (7200 секунд)
    while time.time() - start_time < 7200:
        try:
            # timeout=30 означает "длинный опрос" для экономии ресурсов
            url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={offset}&timeout=30"
            r = requests.get(url, timeout=40)
            updates = r.json().get("result", [])
            
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                
                if str(message.get("chat", {}).get("id")) == str(TG_CHAT_ID):
                    text = message.get("text", "").strip()
                    if text:
                        parts = text.split()
                        if len(parts) >= 1 and parts[0].startswith("http"):
                            search_url = parts[0]
                            pages = 3
                            if len(parts) > 1 and parts[1].isdigit():
                                pages = int(parts[1])
                            
                            send_telegram_msg(f"⏳ Принято! Начинаю парсить:\n{search_url}\nСтраниц: {pages}")
                            return search_url, pages
                        else:
                            send_telegram_msg("❌ Неверный формат. Пришлите ссылку, начинающуюся с http...")
        except Exception as e:
            log.error(f"Ошибка при получении сообщения: {e}")
        
        time.sleep(1)

    send_telegram_msg("⏰ Прошло 2 часа. Вы не прислали ссылку. Сервер выключается.")
    return None, None

# ============================================================
# ПАРСЕР (Playwright)
# ============================================================
async def get_listing_details(page, url, retries=2):
    for attempt in range(retries):
        try:
            if page.is_closed(): return None
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(800)
            page_text = await page.inner_text("body")
            if "Gewerblicher Anbieter" in page_text: return None

            views = 0
            try:
                views_el = page.locator("#viewad-cntr-num").first
                if await views_el.count() > 0:
                    views = int(re.sub(r"\D", "", await views_el.inner_text()) or 0)
            except Exception: pass

            favorites = 0
            fav_match = re.search(r"(\d+(?:\.\d+)*)\s*(?:x|keer|mal)\s*(?:beobachter|gespeichert|bewaard)", page_text, re.IGNORECASE)
            if fav_match: favorites = int(fav_match.group(1).replace(".", ""))

            title = "Без названия"
            try:
                title_el = page.locator("h1#viewad-title").first
                if await title_el.count() > 0: title = (await title_el.inner_text()).strip()
            except Exception: pass

            posted_date = "Дата не указана"
            try:
                m = re.search(r"(\d{2}\.\d{2}\.\d{4})", page_text)
                if m: posted_date = m.group(1)
                elif "Heute" in page_text: posted_date = "Сегодня"
                elif "Gestern" in page_text: posted_date = "Вчера"
            except Exception: pass

            price = "Цена не указана"
            try:
                price_el = page.locator("h2.boxedarticle--price, span.price").first
                if await price_el.count() > 0:
                    raw = (await price_el.inner_text()).strip()
                    if raw: price = raw
            except Exception: pass

            return {"title": title, "link": url, "views": views, "favorites": favorites, "posted_date": posted_date, "price": price}
        except Exception:
            if attempt == retries - 1: return None
            await asyncio.sleep(2)

async def process_batch(links, db_conn):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(viewport={"width": 1366, "height": 768}, locale="de-DE")
        tasks = [await context.new_page() for _ in links]

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[get_listing_details(tasks[i], links[i]) for i in range(len(links))], return_exceptions=True),
                timeout=30.0
            )
        except asyncio.TimeoutError: results = []

        await browser.close()
        for res in results:
            if isinstance(res, dict):
                if record_listing(db_conn, res):
                    msg = (f"🔥 <b>Новое или изменена цена!</b>\n\n"
                           f"📦 {res['title']}\n"
                           f"💰 Цена: {res['price']}\n"
                           f"👁 Просмотры: {res['views']}\n"
                           f"<a href='{res['link']}'>Открыть объявление</a>")
                    send_telegram_msg(msg)

async def run_parser(search_url, max_pages, db_conn):
    log.info(f"Старт парсинга: {search_url} (стр: {max_pages})")
    
    base_url = search_url
    path, query = base_url.split("?", 1) if "?" in base_url else (base_url, "")
    path = re.sub(r"/seite:\d+", "", path)
    base_path, suffix = path.rsplit("/", 1) if "/" in path else (path, "k0")
    
    all_links = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        for page_num in range(1, max_pages + 1):
            url = f"{path}?{query}" if page_num == 1 else f"{base_path}/seite:{page_num}/{suffix}?{query}"
            log.info(f"Сканирую страницу {page_num}/{max_pages}...")
            
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000) 
                
                content = await page.content()
                if "Cloudflare" in content or "Access Denied" in content:
                    send_telegram_msg("🚫 Сайт заблокировал доступ (Cloudflare).")
                    break

                links = page.locator("article.aditem a, a[href*='/s-anzeige/']")
                cnt = await links.count()
                log.info(f"  Найдено ссылок: {cnt}")
                
                for i in range(cnt):
                    href = await links.nth(i).get_attribute("href")
                    if href and not href.startswith("http"):
                        href = "https://www.kleinanzeigen.de" + href
                    if href:
                        clean = href.split("?")[0]
                        if clean not in all_links:
                            all_links.append(clean)
            except Exception as e:
                log.error(f"Ошибка на странице {page_num}: {e}")
        await browser.close()

    log.info(f"Всего собрано уникальных ссылок: {len(all_links)}")
    
    if all_links:
        batch_size = 8
        for i in range(0, len(all_links), batch_size):
            batch = all_links[i:i + batch_size]
            log.info(f"Батч {i//batch_size + 1} из {(len(all_links)-1)//batch_size + 1}")
            await process_batch(batch, db_conn)
            await asyncio.sleep(random.uniform(2.0, 4.0))
    else:
        send_telegram_msg("⚠️ Ссылок не найдено. Возможно, блокировка по IP или неверная ссылка.")

async def main():
    search_url, max_pages = wait_for_user_url()
    if not search_url:
        return

    db_conn = init_db()
    await run_parser(search_url, max_pages, db_conn)
    db_conn.close()
    
    send_telegram_db()
    send_telegram_msg("✅ Парсинг завершен. База данных отправлена выше. Сервер выключается.")
    log.info("Работа завершена.")

if __name__ == "__main__":
    asyncio.run(main())