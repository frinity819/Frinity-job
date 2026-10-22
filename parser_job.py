import os
import re
import sqlite3
import random
import datetime
import asyncio
import logging
import requests
import time
import base64
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from supabase import create_client, Client

# ============================================================
# НАСТРОЙКИ
# ============================================================
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
GIST_ID = os.environ.get("GIST_ID", "")
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DB_PATH = "listings.db"

# --- НАСТРОЙКИ ПО УМОЛЧАНИЮ (если не прислали ссылку) ---
DEFAULT_SEARCH_URL = "https://www.kleinanzeigen.de/s-multimedia-elektronik/preis:120:950/c161"
DEFAULT_PAGES = 50
WAIT_TIMEOUT_MINUTES = 20 # Ждем 20 минут перед тем, как включить дефолтную ссылку

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# Инициализация Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# РАБОТА С GIST (Base64)
# ============================================================
def download_db_from_gist():
    if not GIST_ID or not GIST_TOKEN:
        log.warning("GIST не настроен. Пропускаем скачивание.")
        return
    log.info("Скачиваем базу из Gist...")
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        
        gist_data = resp.json()
        file_data = gist_data["files"].get("listings.db")
        if file_data:
            content = file_data.get("content")
            if not content:
                raw_url = file_data.get("raw_url")
                content_b64 = requests.get(raw_url).text
            else:
                content_b64 = content
            
            db_bytes = base64.b64decode(content_b64)
            with open(DB_PATH, "wb") as f:
                f.write(db_bytes)
            log.info("База успешно скачана из Gist.")
        else:
            log.warning("В Gist нет файла listings.db. Создаем новую базу.")
    except Exception as e:
        log.error(f"Ошибка скачивания из Gist: {e}")

def upload_db_to_gist():
    if not GIST_ID or not GIST_TOKEN:
        log.warning("GIST не настроен. Пропускаем загрузку.")
        return
    log.info("Загружаем базу в Gist...")
    try:
        with open(DB_PATH, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode('utf-8')
        
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
        data = {"files": {"listings.db": {"content": content_b64, "encoding": "base64"}}}
        
        resp = requests.patch(url, headers=headers, json=data, timeout=30)
        resp.raise_for_status()
        log.info("База успешно загружена в Gist.")
    except Exception as e:
        log.error(f"Ошибка загрузки в Gist: {e}")

# ============================================================
# БАЗА ДАННЫХ (SQLite)
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
# СИНХРОНИЗАЦИЯ С SUPABASE
# ============================================================
def sync_to_supabase():
    if not supabase:
        log.warning("Supabase не настроен. Пропускаем.")
        return
    log.info("Начинаем синхронизацию с Supabase...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT link, title, first_seen FROM listings")
        listings = [dict(row) for row in cursor.fetchall()]
        if listings:
            for i in range(0, len(listings), 500):
                supabase.table('listings').upsert(listings[i:i+500], on_conflict='link').execute()
            log.info(f"✅ Синхронизировано {len(listings)} объявлений.")

        res = supabase.table('snapshots').select('snapshot_at').order('snapshot_at', desc=True).limit(1).execute()
        last_cloud = res.data[0]['snapshot_at'] if res.data else "1970-01-01T00:00:00"
        
        cursor.execute("SELECT link, price, price_numeric, views, favorites, posted_date, snapshot_at FROM snapshots WHERE snapshot_at > ?", (last_cloud,))
        snapshots = [dict(row) for row in cursor.fetchall()]
        
        if snapshots:
            for i in range(0, len(snapshots), 500):
                supabase.table('snapshots').insert(snapshots[i:i+500]).execute()
            log.info(f"✅ Синхронизировано {len(snapshots)} новых снимков.")
        else:
            log.info("Новых снимков для Supabase нет.")
    except Exception as e:
        log.error(f"❌ Ошибка Supabase: {e}")
    finally:
        conn.close()

# ============================================================
# TELEGRAM API
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
    log.info(f"Ожидание ответа от пользователя в Telegram (макс. {WAIT_TIMEOUT_MINUTES} мин)...")
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset=-1", timeout=10)
        updates = r.json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else 0
    except Exception:
        offset = 0

    send_telegram_msg(f"🤖 <b>Бот запущен!</b>\n\nОтправьте мне ссылку на поиск Kleinanzeigen.\n"
                      f"Вы можете добавить количество страниц через пробел.\n"
                      f"Пример: <code>https://www.kleinanzeigen.de/s-... 3</code>\n\n"
                      f"⏳ У вас есть {WAIT_TIMEOUT_MINUTES} минут. Если не ответите, бот запустит парсинг по умолчанию.")

    start_time = time.time()
    while time.time() - start_time < (WAIT_TIMEOUT_MINUTES * 60):
        try:
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

    # Если время вышло и ссылку не прислали — запускаем дефолт
    log.info("Время ожидания вышло. Запускаем парсинг по умолчанию.")
    send_telegram_msg(f"⏰ Время вышло. Запускаю парсинг по умолчанию:\n{DEFAULT_SEARCH_URL}\nСтраниц: {DEFAULT_PAGES}")
    return DEFAULT_SEARCH_URL, DEFAULT_PAGES

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
            fav_match = re.search(r"(\d+(?:\.\d+)*)\s*(?:x|mal|keer)?\s*(?:gespeichert|beobachter|bewaard|favoriten)", page_text, re.IGNORECASE)
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
                           f"👁 Просмотры: {res['views']} | ⭐ В избранном: {res['favorites']}\n"
                           f"<a href='{res['link']}'>Открыть объявление</a>")
                    send_telegram_msg(msg)

async def run_parser(search_url, max_pages, db_conn):
    log.info(f"Старт парсинга: {search_url} (стр: {max_pages})")
    
    parsed_url = urlparse(search_url)
    domain = f"{parsed_url.scheme}://{parsed_url.netloc}" 
    path_parts = [p for p in parsed_url.path.split("/") if p]
    
    all_links = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768}, 
            locale="de-DE"
        )
        page = await context.new_page()
        
        for page_num in range(1, max_pages + 1):
            if page_num == 1:
                url = search_url
            else:
                if len(path_parts) >= 3:
                    category = path_parts[0]
                    item_id = path_parts[-1]
                    filters = path_parts[1:-1]
                    
                    parts_to_join = [domain, category] + filters + [f"seite:{page_num}", item_id]
                    url = "/".join(parts_to_join)
                else:
                    url = f"{domain}/{path_parts[0]}/seite:{page_num}"
                    
                if parsed_url.query:
                    url += f"?{parsed_url.query}"
            
            log.info(f"Сканирую страницу {page_num}/{max_pages}: {url}")
            
            page_links_found = 0
            for attempt in range(2):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(3500) 
                    
                    content = await page.content()
                    if "Cloudflare" in content or "Access Denied" in content or "captcha" in content.lower():
                        send_telegram_msg(f"🚫 Сайт заблокировал доступ (Cloudflare) на странице {page_num}.")
                        return

                    links = page.locator("article.aditem a, a[href*='/s-anzeige/']")
                    cnt = await links.count()
                    
                    if cnt > 0:
                        log.info(f"  Найдено ссылок: {cnt}")
                        page_links_found = cnt
                        for i in range(cnt):
                            href = await links.nth(i).get_attribute("href")
                            if href and not href.startswith("http"):
                                href = "https://www.kleinanzeigen.de" + href
                            if href:
                                clean = href.split("?")[0]
                                if clean not in all_links:
                                    all_links.append(clean)
                        break
                    else:
                        log.info(f"  Попытка {attempt+1}: 0 ссылок. Ждем 5 сек...")
                        await page.wait_for_timeout(5000)
                        
                except Exception as e:
                    log.error(f"Ошибка на странице {page_num} (попытка {attempt+1}): {e}")
                    await page.wait_for_timeout(5000)

            if page_links_found == 0:
                log.info("  Страница пуста после 2 попыток. Похоже, объявления закончились.")
                break
                
        await browser.close()

    log.info(f"Всего собрано уникальных ссылок: {len(all_links)}")
    
    # ============================================================
    # ФИЛЬТРАЦИЯ ДУБЛИКАТОВ (Проверка за последние 5 часов)
    # ============================================================
    if all_links:
        cursor = db_conn.cursor()
        # Вычисляем время 5 часов назад
        time_threshold = (datetime.datetime.now() - datetime.timedelta(hours=5)).isoformat(timespec="seconds")
        
        cursor.execute("SELECT DISTINCT link FROM snapshots WHERE snapshot_at > ?", (time_threshold,))
        recently_checked = {row[0] for row in cursor.fetchall()}
        
        filtered_links = [link for link in all_links if link not in recently_checked]
        
        skipped_count = len(all_links) - len(filtered_links)
        log.info(f"🔍 Фильтрация: пропущено {skipped_count} недавно проверенных ссылок (менее 5 часов назад).")
        log.info(f"🎯 Осталось ссылок для парсинга: {len(filtered_links)}")
        
        all_links = filtered_links
    # ============================================================

    if all_links:
        batch_size = 8
        for i in range(0, len(all_links), batch_size):
            batch = all_links[i:i + batch_size]
            log.info(f"Батч {i//batch_size + 1} из {(len(all_links)-1)//batch_size + 1}")
            await process_batch(batch, db_conn)
            await asyncio.sleep(random.uniform(2.0, 4.0))
    else:
        log.info("Нет новых или требующих обновления объявлений.")
        send_telegram_msg("ℹ️ Парсинг завершен: новых объявлений нет, а старые были проверены недавно.")

async def main():
    download_db_from_gist()
    
    search_url, max_pages = wait_for_user_url()
    
    db_conn = init_db()
    await run_parser(search_url, max_pages, db_conn)
    db_conn.close()
    
    upload_db_to_gist()
    send_telegram_db()
    sync_to_supabase()
    
    send_telegram_msg("✅ Парсинг завершен. База данных обновлена, отправлена в Telegram и синхронизирована с Supabase.")

if __name__ == "__main__":
    asyncio.run(main())
