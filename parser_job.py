import os
import re
import sqlite3
import random
import datetime
import asyncio
import logging
import requests
import time
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from supabase import create_client, Client

# ============================================================
# НАСТРОЙКИ
# ============================================================
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DB_PATH = "listings.db"

# --- НАСТРОЙКИ ПО УМОЛЧАНИЮ ---
DEFAULT_SEARCH_URL = "https://www.kleinanzeigen.de/s-multimedia-elektronik/preis:120:950/c161"
DEFAULT_PAGES = 50
WAIT_TIMEOUT_MINUTES = 20 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# БАЗА ДАННЫХ (Временная локальная SQLite)
# ============================================================
def init_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH) # Удаляем старую, чтобы начать с чистой
        
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS listings (link TEXT PRIMARY KEY, title TEXT, first_seen TEXT, last_checked TEXT)")
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
    
    # Сохраняем во временную локальную базу
    conn.execute("""
        INSERT INTO listings (link, title, first_seen, last_checked) 
        VALUES (?, ?, ?, ?) 
        ON CONFLICT(link) DO UPDATE SET last_checked=excluded.last_checked
    """, (item["link"], item["title"], now, now))
    
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
    conn.commit()
    return False

# ============================================================
# СИНХРОНИЗАЦИЯ С SUPABASE
# ============================================================
def get_recently_checked_from_supabase():
    """Скачивает из облака список ссылок и время их проверки"""
    if not supabase: return {}
    log.info("Запрашиваем время последних проверок из Supabase...")
    try:
        # Берем все ссылки и их last_checked (Supabase отдает максимум 1000 за раз)
        # Для простоты берем последние 5000 записей
        res = supabase.table('listings').select('link, last_checked').order('last_checked', desc=True).limit(5000).execute()
        checked_dict = {row['link']: row['last_checked'] for row in res.data if row.get('last_checked')}
        log.info(f"Получено {len(checked_dict)} отметок о проверках из Supabase.")
        return checked_dict
    except Exception as e:
        log.error(f"Ошибка получения данных из Supabase: {e}")
        return {}

def sync_to_supabase():
    if not supabase:
        return
    log.info("Начинаем синхронизацию с Supabase...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT link, title, first_seen, last_checked FROM listings")
        listings = [dict(row) for row in cursor.fetchall()]
        if listings:
            for i in range(0, len(listings), 500):
                supabase.table('listings').upsert(listings[i:i+500], on_conflict='link').execute()
            log.info(f"✅ Обновлено {len(listings)} объявлений в облаке.")

        # В локальной базе лежат ТОЛЬКО снимки за текущий запуск. 
        # Поэтому мы просто отправляем их все.
        cursor.execute("SELECT link, price, price_numeric, views, favorites, posted_date, snapshot_at FROM snapshots")
        snapshots = [dict(row) for row in cursor.fetchall()]
        
        if snapshots:
            for i in range(0, len(snapshots), 500):
                supabase.table('snapshots').insert(snapshots[i:i+500]).execute()
            log.info(f"✅ Добавлено {len(snapshots)} новых снимков цен.")
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
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}, timeout=10)
    except Exception: pass

def send_telegram_db():
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        with open(DB_PATH, 'rb') as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", 
                          files={'document': ('listings.db', f)}, data={'chat_id': TG_CHAT_ID}, timeout=30)
    except Exception: pass

async def wait_for_user_url():
    log.info(f"Ожидание ответа от пользователя в Telegram (макс. {WAIT_TIMEOUT_MINUTES} мин)...")
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset=-1", timeout=10)
        updates = r.json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else 0
    except Exception:
        offset = 0

    send_telegram_msg(f"🤖 <b>Бот запущен!</b>\n\nОтправьте мне ссылку.\n⏳ У вас есть {WAIT_TIMEOUT_MINUTES} минут.")

    start_time = time.time()
    last_notification = 0

    while time.time() - start_time < (WAIT_TIMEOUT_MINUTES * 60):
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={offset}&timeout=10"
            r = requests.get(url, timeout=15)
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
        except Exception:
            pass 
        
        elapsed_minutes = (time.time() - start_time) / 60
        if elapsed_minutes > (WAIT_TIMEOUT_MINUTES - 5) and last_notification == 0:
            send_telegram_msg("⏰ Осталось 5 минут. Если не пришлете ссылку, начну парсинг по умолчанию.")
            last_notification = 1

        await asyncio.sleep(2)

    log.info("Время ожидания вышло. Запускаем парсинг по умолчанию.")
    send_telegram_msg(f"⏰ Время вышло! Запускаю парсинг по умолчанию.")
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
                if await views_el.count() > 0: views = int(re.sub(r"\D", "", await views_el.inner_text()) or 0)
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
                if await price_el.count() > 0: price = (await price_el.inner_text()).strip()
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
                asyncio.gather(*[get_listing_details(tasks[i], links[i]) for i in range(len(links))], return_exceptions=True), timeout=30.0)
        except asyncio.TimeoutError: results = []

        await browser.close()
        for res in results:
            if isinstance(res, dict):
                if record_listing(db_conn, res):
                    send_telegram_msg(f"🔥 <b>Новое или изменена цена!</b>\n\n📦 {res['title']}\n💰 Цена: {res['price']}\n<a href='{res['link']}'>Открыть</a>")

async def run_parser(search_url, max_pages, db_conn, recently_checked):
    log.info(f"Старт парсинга: {search_url} (стр: {max_pages})")
    
    parsed_url = urlparse(search_url)
    domain = f"{parsed_url.scheme}://{parsed_url.netloc}" 
    path_parts = [p for p in parsed_url.path.split("/") if p]
    
    all_links = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768}, locale="de-DE")
        page = await context.new_page()
        
        for page_num in range(1, max_pages + 1):
            if page_num == 1:
                url = search_url
            else:
                if len(path_parts) >= 3:
                    category, item_id = path_parts[0], path_parts[-1]
                    filters = path_parts[1:-1]
                    url = "/".join([domain, category] + filters + [f"seite:{page_num}", item_id])
                else:
                    url = f"{domain}/{path_parts[0]}/seite:{page_num}"
                if parsed_url.query: url += f"?{parsed_url.query}"
            
            log.info(f"Сканирую страницу {page_num}/{max_pages}...")
            
            page_links_found = 0
            for attempt in range(2):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(3500) 
                    
                    if "Cloudflare" in await page.content() or "captcha" in (await page.content()).lower():
                        log.warning("🚫 Блокировка Cloudflare.")
                        return

                    links = page.locator("article.aditem a, a[href*='/s-anzeige/']")
                    cnt = await links.count()
                    
                    if cnt > 0:
                        log.info(f"  Найдено ссылок: {cnt}")
                        page_links_found = cnt
                        for i in range(cnt):
                            href = await links.nth(i).get_attribute("href")
                            if href and not href.startswith("http"): href = "https://www.kleinanzeigen.de" + href
                            if href:
                                clean = href.split("?")[0]
                                if clean not in all_links: all_links.append(clean)
                        break
                    else:
                        log.info(f"  Попытка {attempt+1}: 0 ссылок. Ждем...")
                        await page.wait_for_timeout(5000)
                except Exception as e:
                    log.error(f"Ошибка на странице {page_num}: {e}")
                    await page.wait_for_timeout(5000)

            if page_links_found == 0:
                log.info("  Объявления закончились.")
                break
        await browser.close()

    log.info(f"Всего собрано уникальных ссылок: {len(all_links)}")
    
    # ============================================================
    # ФИЛЬТРАЦИЯ НА ОСНОВЕ ДАННЫХ ИЗ SUPABASE
    # ============================================================
    if all_links:
        now = datetime.datetime.now()
        time_threshold = (now - datetime.timedelta(hours=5)).isoformat(timespec="seconds")
        
        filtered_links = []
        for link in all_links:
            if link not in recently_checked:
                # Если ссылки вообще нет в базе — берем
                filtered_links.append(link)
            else:
                # Если ссылка есть, проверяем её last_checked
                last_check_str = recently_checked[link]
                if last_check_str < time_threshold:
                    # Если проверяли более 5 часов назад — берем
                    filtered_links.append(link)
        
        log.info(f"🔍 Фильтрация: пропущено {len(all_links) - len(filtered_links)} недавно проверенных ссылок.")
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

async def main():
    db_conn = init_db()
    
    # Скачиваем историю проверок из облака
    recently_checked = get_recently_checked_from_supabase()
    
    search_url, max_pages = await wait_for_user_url()
    
    await run_parser(search_url, max_pages, db_conn, recently_checked)
    db_conn.close()
    
    # Отправляем в ТГ временную базу и синхронизируем с облаком
    send_telegram_db()
    sync_to_supabase()
    
    send_telegram_msg("✅ Парсинг завершен. Все данные загружены в Supabase.")

if __name__ == "__main__":
    asyncio.run(main())
