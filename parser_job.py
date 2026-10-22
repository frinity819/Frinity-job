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
import base64
from playwright.async_api import async_playwright
from supabase import create_client, Client

# ============================================================
# НАСТРОЙКИ (Читаются из GitHub Secrets)
# ============================================================
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
GIST_ID = os.environ.get("GIST_ID", "")
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DB_PATH = "listings.db"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# Инициализация клиента Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# РАБОТА С GIST (Сохранение базы в облако GitHub)
# ============================================================
def download_db_from_gist():
    if not GIST_ID or not GIST_TOKEN:
        log.warning("Gist не настроен, пропускаем скачивание.")
        return

    log.info("Скачиваем базу данных из Gist...")
    headers = {"Authorization": f"token {GIST_TOKEN}"}
    url = f"https://api.github.com/gists/{GIST_ID}"
    
    try:
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        gist_data = r.json()
        file_data = gist_data['files'].get('listings.db')
        
        if file_data and not file_data.get('truncated'):
            raw_url = file_data['raw_url']
            db_content = requests.get(raw_url).content
        elif file_data:
            # Если файл большой, GitHub может отдать его урезанным, качаем напрямую
            db_content = requests.get(file_data['raw_url']).content
        else:
            log.info("Файл базы данных не найден в Gist. Будет создан новый.")
            return

        with open(DB_PATH, 'wb') as f:
            f.write(db_content)
        log.info("✅ База данных успешно скачана из Gist.")
    except Exception as e:
        log.error(f"❌ Ошибка при скачивании из Gist: {e}")

def upload_db_to_gist():
    if not GIST_ID or not GIST_TOKEN:
        return
    if not os.path.exists(DB_PATH):
        return

    log.info("Загружаем обновленную базу данных в Gist...")
    headers = {"Authorization": f"token {GIST_TOKEN}"}
    url = f"https://api.github.com/gists/{GIST_ID}"
    
    try:
        with open(DB_PATH, 'rb') as f:
            content = f.read()
        encoded_content = base64.b64encode(content).decode('utf-8')
        
        payload = {
            "files": {
                "listings.db": {
                    "content": encoded_content,
                    "encoding": "base64"
                }
            }
        }
        r = requests.patch(url, headers=headers, json=payload)
        r.raise_for_status()
        log.info("✅ База данных успешно загружена в Gist.")
    except Exception as e:
        log.error(f"❌ Ошибка при загрузке в Gist: {e}")

# ============================================================
# ЛОКАЛЬНАЯ БАЗА ДАННЫХ (SQLite)
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS listings (link TEXT PRIMARY KEY, title TEXT, first_seen TEXT)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT,
            price TEXT,
            price_numeric REAL,
            views INTEGER,
            favorites INTEGER,
            posted_date TEXT,
            snapshot_at TEXT,
            FOREIGN KEY(link) REFERENCES listings(link)
        )
    """)
    conn.commit()
    return conn

def parse_price_numeric(price_str):
    if not price_str:
        return 0.0
    # Оставляем только цифры и запятые/точки
    clean_str = re.sub(r'[^\d,.]', '', price_str).replace(',', '.')
    try:
        # Берем первое число
        match = re.search(r'\d+(\.\d+)?', clean_str)
        return float(match.group()) if match else 0.0
    except:
        return 0.0

def record_listing(conn, item):
    cursor = conn.cursor()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # Вставляем в listings, если такой ссылки еще нет
    cursor.execute("""
        INSERT OR IGNORE INTO listings (link, title, first_seen) 
        VALUES (?, ?, ?)
    """, (item['link'], item['title'], now_iso))
    
    # Вставляем снимок (историю) при каждом запуске
    cursor.execute("""
        INSERT INTO snapshots (link, price, price_numeric, views, favorites, posted_date, snapshot_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        item['link'], item['price'], item['price_numeric'], 
        item['views'], item['favorites'], item['posted_date'], now_iso
    ))
    conn.commit()

# ============================================================
# TELEGRAM API
# ============================================================
def send_telegram_msg(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        log.error(f"Ошибка отправки в ТГ: {e}")

def send_telegram_db():
    if not TG_TOKEN or not TG_CHAT_ID or not os.path.exists(DB_PATH):
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
        with open(DB_PATH, 'rb') as f:
            requests.post(url, data={"chat_id": TG_CHAT_ID}, files={"document": f})
    except Exception as e:
        log.error(f"Ошибка отправки файла в ТГ: {e}")

def wait_for_user_url():
    log.info("Ожидание ссылки от пользователя в Telegram...")
    last_update_id = 0
    # Ждем ссылку не более 15 минут (чтобы сервер не висел вечно)
    timeout = time.time() + 900 
    
    while time.time() < timeout:
        if not TG_TOKEN:
            # Если токена нет (локальный запуск), берем тестовую ссылку
            return "https://www.kleinanzeigen.de/s-suchanfrage.html?keywords=iPhone", 5
            
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10"
            r = requests.get(url).json()
            
            if r.get("ok") and r["result"]:
                for update in r["result"]:
                    last_update_id = update["update_id"]
                    if "message" in update and "text" in update["message"]:
                        text = update["message"]["text"]
                        # Ищем ссылку kleinanzeigen
                        urls = re.findall(r'(https?://[^\s]+)', text)
                        if urls:
                            target_url = urls[0]
                            # Ищем число страниц (опционально, по умолчанию 3)
                            pages = 3
                            match = re.search(r'страниц:?\s*(\d+)', text)
                            if match:
                                pages = int(match.group(1))
                            return target_url, pages
        except Exception as e:
            log.error(f"Ошибка Telegram getUpdates: {e}")
            
        time.sleep(2)
    return None, 0

# ============================================================
# ПАРСЕР (Playwright)
# ============================================================
async def get_listing_details(page, url, retries=2):
    for attempt in range(retries):
        try:
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(1.0, 2.0)) # Защита от бана
            
            # Заголовок
            title_el = await page.query_selector("#viewad-title")
            title = await title_el.inner_text() if title_el else "Без названия"
            title = title.strip()

            # Цена
            price_el = await page.query_selector("#viewad-price")
            price_text = await price_el.inner_text() if price_el else ""
            price_num = parse_price_numeric(price_text)

            # Просмотры
            views_el = await page.query_selector("#viewad-cntr-num")
            views = 0
            if views_el:
                views_text = await views_el.inner_text()
                views_match = re.search(r'\d+', views_text)
                views = int(views_match.group()) if views_match else 0

            # Дата
            date_el = await page.query_selector("#viewad-extra-info > div")
            posted_date = await date_el.inner_text() if date_el else ""

            return {
                "link": url,
                "title": title,
                "price": price_text.strip(),
                "price_numeric": price_num,
                "views": views,
                "favorites": 0, # Количество избранных часто скрыто, ставим 0
                "posted_date": posted_date.strip()
            }
        except Exception as e:
            if attempt == retries - 1:
                log.warning(f"Не удалось спарсить {url}: {e}")
                return None
            await asyncio.sleep(2)

async def process_batch(page, links, db_conn):
    for link in links:
        data = await get_listing_details(page, link)
        if data:
            record_listing(db_conn, data)
            log.info(f"Сохранено: {data['title'][:30]}... | Цена: {data['price']}")

async def run_parser(search_url, max_pages, db_conn):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        
        all_links = []
        for page_num in range(1, max_pages + 1):
            # Добавляем пагинацию к URL
            paginated_url = search_url
            if page_num > 1:
                if "?" in search_url:
                    paginated_url = search_url + f"&page={page_num}"
                else:
                    paginated_url = search_url + f"?page={page_num}"
                    
            log.info(f"Парсим страницу поиска {page_num}: {paginated_url}")
            try:
                await page.goto(paginated_url, timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_selector('article.aditem', timeout=10000)
                
                # Собираем ссылки
                items = await page.query_selector_all('article.aditem a.ellipsis')
                for item in items:
                    href = await item.get_attribute('href')
                    if href:
                        full_url = "https://www.kleinanzeigen.de" + href if href.startswith('/') else href
                        all_links.append(full_url)
            except Exception as e:
                log.error(f"Ошибка на странице {page_num}: {e}")
                break

        # Убираем дубликаты ссылок
        all_links = list(dict.fromkeys(all_links))
        log.info(f"Найдено уникальных объявлений: {len(all_links)}")

        # Переходим в каждое объявление для сбора деталей (пачками)
        batch_size = 10
        for i in range(0, len(all_links), batch_size):
            batch = all_links[i:i+batch_size]
            await process_batch(page, batch, db_conn)
            await asyncio.sleep(random.uniform(2.0, 4.0)) # Пауза между пачками

        await browser.close()

# ============================================================
# СИНХРОНИЗАЦИЯ С SUPABASE
# ============================================================
def sync_to_supabase():
    if not supabase:
        log.warning("Supabase не настроен. Пропускаем синхронизацию.")
        return

    log.info("Начинаем синхронизацию с Supabase...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # 1. СИНХРОНИЗАЦИЯ ОБЪЯВЛЕНИЙ (upsert безопасен для дубликатов)
        cursor.execute("SELECT link, title, first_seen FROM listings")
        listings = [dict(row) for row in cursor.fetchall()]
        
        if listings:
            log.info(f"Отправляем {len(listings)} объявлений...")
            for i in range(0, len(listings), 500):
                batch = listings[i:i+500]
                supabase.table('listings').upsert(batch, on_conflict='link').execute()
            log.info("✅ Таблица listings синхронизирована.")

        # 2. СИНХРОНИЗАЦИЯ ИСТОРИИ (snapshots)
        # Чтобы не заливать каждый раз одно и то же, проверим, что уже есть в облаке
        # Получаем последнюю дату снимка из Supabase
        res = supabase.table('snapshots').select('snapshot_at').order('snapshot_at', desc=True).limit(1).execute()
        last_cloud_snapshot = res.data[0]['snapshot_at'] if res.data else "1970-01-01T00:00:00+00:00"
        
        # Берем из локальной базы только те снимки, которые новее, чем в облаке
        cursor.execute("SELECT link, price, price_numeric, views, favorites, posted_date, snapshot_at FROM snapshots WHERE snapshot_at > ?", (last_cloud_snapshot,))
        snapshots = [dict(row) for row in cursor.fetchall()]
        
        if snapshots:
            log.info(f"Отправляем {len(snapshots)} новых снимков истории...")
            for i in range(0, len(snapshots), 500):
                batch = snapshots[i:i+500]
                supabase.table('snapshots').insert(batch).execute()
            log.info("✅ Таблица snapshots синхронизирована.")
        else:
            log.info("Новых снимков для Supabase нет.")

    except Exception as e:
        log.error(f"❌ Ошибка синхронизации с Supabase: {e}")
    finally:
        conn.close()

# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    # 1. Скачиваем базу из Gist (если есть)
    download_db_from_gist()
    
    # 2. Ждем параметры от пользователя (URL)
    search_url, max_pages = wait_for_user_url()
    if not search_url:
        log.info("Ссылка не получена. Завершаем работу.")
        return
        
    send_telegram_msg(f"🚀 Начинаю парсинг: {search_url}\nСтраниц: {max_pages}")
    
    # 3. Инициализируем БД и парсим
    db_conn = init_db()
    await run_parser(search_url, max_pages, db_conn)
    db_conn.close()

    # 4. Загружаем базу обратно в Gist и отправляем в ТГ
    upload_db_to_gist()
    send_telegram_db()

    # 5. Синхронизируем с Supabase (НОВЫЙ ШАГ)
    sync_to_supabase()

    send_telegram_msg("✅ Парсинг завершен. База данных обновлена, отправлена в Telegram и синхронизирована с Supabase.")

if __name__ == "__main__":
    asyncio.run(main())
