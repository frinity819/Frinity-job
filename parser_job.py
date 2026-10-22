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
from supabase import create_client, Client # <-- ДОБАВЛЕНО

# ============================================================
# НАСТРОЙКИ
# ============================================================
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
GIST_ID = os.environ.get("GIST_ID", "")
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
# --- ДОБАВЛЕНО ДЛЯ SUPABASE ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DB_PATH = "listings.db"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# Инициализация клиента Supabase (если ключи заданы)
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# РАБОТА С GIST (Скачивание и Загрузка) - БЕЗ ИЗМЕНЕНИЙ
# ============================================================
def download_db_from_gist():
    # ... (ваш существующий код) ...
    pass

def upload_db_to_gist():
    # ... (ваш существующий код) ...
    pass

# ============================================================
# БАЗА ДАННЫХ
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
    # ... (ваш существующий код) ...
    pass

def record_listing(conn, item):
    # ... (ваш существующий код) ...
    pass

# ============================================================
# TELEGRAM API - БЕЗ ИЗМЕНЕНИЙ
# ============================================================
def send_telegram_msg(text):
    # ... (ваш существующий код) ...
    pass

def send_telegram_db():
    # ... (ваш существующий код) ...
    pass

def wait_for_user_url():
    # ... (ваш существующий код) ...
    pass

# ============================================================
# ПАРСЕР (Playwright) - БЕЗ ИЗМЕНЕНИЙ
# ============================================================
async def get_listing_details(page, url, retries=2):
    # ... (ваш существующий код) ...
    pass

async def process_batch(links, db_conn):
    # ... (ваш существующий код) ...
    pass

async def run_parser(search_url, max_pages, db_conn):
    # ... (ваш существующий код) ...
    pass

# ============================================================
# СИНХРОНИЗАЦИЯ С SUPABASE (НОВАЯ ФУНКЦИЯ)
# ============================================================
def sync_to_supabase():
    """Отправляет новые записи из локальной базы в Supabase."""
    if not supabase:
        log.warning("Supabase не настроен. Пропускаем синхронизацию.")
        return

    log.info("Начинаем синхронизацию с Supabase...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # 1. Синхронизируем объявления (listings)
        cursor.execute("SELECT link, title, first_seen FROM listings")
        listings_rows = [dict(row) for row in cursor.fetchall()]
        if listings_rows:
            # Используем upsert, чтобы не дублировать существующие ссылки
            for i in range(0, len(listings_rows), 500):
                batch = listings_rows[i:i+500]
                supabase.table('listings').upsert(batch, on_conflict='link').execute()
            log.info(f"✅ Синхронизировано {len(listings_rows)} объявлений в Supabase.")

        # 2. Синхронизируем снимки (snapshots)
        # Для простоты отправляем все снимки, которые есть локально, но в идеале
        # нужно отслеживать, какие уже были отправлены.
        cursor.execute("SELECT link, price, price_numeric, views, favorites, posted_date, snapshot_at FROM snapshots")
        snapshots_rows = [dict(row) for row in cursor.fetchall()]
        if snapshots_rows:
            for i in range(0, len(snapshots_rows), 500):
                batch = snapshots_rows[i:i+500]
                # Здесь используем insert, так как снимки могут повторяться по link, но иметь разное время
                supabase.table('snapshots').insert(batch).execute()
            log.info(f"✅ Синхронизировано {len(snapshots_rows)} снимков в Supabase.")

    except Exception as e:
        log.error(f"❌ Ошибка синхронизации с Supabase: {e}")
    finally:
        conn.close()

# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    # 1. Скачиваем базу из Gist
    download_db_from_gist()
    # 2. Ждем параметры
    search_url, max_pages = wait_for_user_url()
    if not search_url:
        return
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
