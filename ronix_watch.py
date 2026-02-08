import os, re, json, time, hashlib, random
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_LIST_URL = os.getenv("RONIX_LIST_URL", "https://www.ronix.ir/power-tools/")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHANNEL_ID = os.getenv("TG_CHANNEL_ID")  # @ir_ronix

PRICE_BOT = "@SsAaSsHhAaRr_bot"
PV_LINE = f"\n💬 برای استعلام قیمت به ربات پیام بدین\n{PRICE_BOT}"

# ✅ Batch control
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))                 # چند محصول در هر اجرا
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "10"))   # چند پست در هر اجرا

# ✅ Delay control (قابل تنظیم از env هم هست)
LIST_PAGE_SLEEP = float(os.getenv("LIST_PAGE_SLEEP", "2.5"))
PRODUCT_SLEEP_NORMAL = float(os.getenv("PRODUCT_SLEEP_NORMAL", "2.5"))
PRODUCT_SLEEP_EVERY10 = float(os.getenv("PRODUCT_SLEEP_EVERY10", "4.0"))
TG_SEND_SLEEP = float(os.getenv("TG_SEND_SLEEP", "2.5"))

if not BOT_TOKEN or not CHANNEL_ID:
    raise SystemExit("Missing TG_BOT_TOKEN or TG_CHANNEL_ID (set as GitHub Secrets).")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; RonixWatcher/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.7,en;q=0.6",
    "Connection": "keep-alive",
})

def _sleep(base: float):
    # jitter برای طبیعی‌تر شدن درخواست‌ها
    time.sleep(base + random.uniform(0.0, 0.7))

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("products", {})
                    data.setdefault("cursor", 0)
                    return data
            except json.JSONDecodeError:
                pass
    return {"products": {}, "cursor": 0}

def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def tg_send_message(text):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = session.post(api, json={
        "chat_id": CHANNEL_ID,
        "text": text,
        "disable_web_page_preview": False
    }, timeout=30)
    r.raise_for_status()

def tg_send_photo(photo_url, caption):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    r = session.post(api, json={
        "chat_id": CHANNEL_ID,
        "photo": photo_url,
        "caption": caption
    }, timeout=30)
    if not r.ok:
        tg_send_message(caption)

def fetch(url: str) -> str:
    """
    ✅ مقاوم‌تر:
    - 5 تلاش
    - backoff افزایشی
    - هندل بهتر برای timeout/429/5xx
    """
    last_err = None
    for attempt in range(1, 6):  # 5 tries
        try:
            r = session.get(url, timeout=90)

            # اگر ریت‌لیمیت یا خطای سرور بود، retry
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)

            r.raise_for_status()
            return r.text

        except Exception as e:
            last_err = e
            wait = min(4 * attempt, 18)  # 4s, 8s, 12s, 16s, 18s
            print(f"FETCH RETRY {attempt}/5: {url} -> {e} | sleep {wait}s")
            _sleep(wait)

    raise last_err

def normalize_url(u: str) -> str:
    p = urlparse(u)
    return p._replace(query="").geturl()

def extract_product_links_from_list(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    links = set()
    page_links = set()

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        abs_u = urljoin(base_url, href)

        if "/product/" in href:
            links.add(normalize_url(abs_u))

        if "page=" in href or "/page/" in href:
            page_links.add(abs_u)

    return sorted(links), sorted(page_links)

def parse_product_page(url: str):
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else url

    full_text = soup.get_text("\n", strip=True)

    model = None
    m = re.search(r"مدل\s*[:：]\s*([A-Z0-9\-]+)", full_text)
    if m:
        model = m.group(1)

    img = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        img = og["content"].strip()

    key_blob = (title + "|" + (model or "") + "|" + full_text[:2000]).encode("utf-8")
    fingerprint = hashlib.sha256(key_blob).hexdigest()

    return {
        "url": url,
        "title": title,
        "model": model,
        "img": img,
        "fingerprint": fingerprint,
    }

def crawl_all_products(start_url: str, max_pages: int = 60):
    seen_pages = set()
    to_visit_pages = [start_url]
    product_urls = set()

    while to_visit_pages and len(seen_pages) < max_pages:
        page_url = to_visit_pages.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)

        try:
            html = fetch(page_url)
        except Exception as e:
            print(f"SKIP LIST PAGE (failed): {page_url} -> {e}")
            continue

        products, page_links = extract_product_links_from_list(html, page_url)

        for p in products:
            product_urls.add(p)

        for pl in page_links:
            if urlparse(pl).netloc == "www.ronix.ir":
                to_visit_pages.append(pl)

        _sleep(LIST_PAGE_SLEEP)

    return sorted(product_urls)

def main():
    state = load_state()
    prev = state.get("products", {})
    cursor = int(state.get("cursor", 0))

    product_list = crawl_all_products(BASE_LIST_URL)

    if not product_list:
        tg_send_message("⚠️ نتونستم لیست محصولات رو بگیرم (احتمالاً سایت کند/بلاک). دفعه بعد دوباره تلاش می‌کنم.")
        return

    if cursor >= len(product_list):
        cursor = 0

    batch = product_list[cursor: cursor + BATCH_SIZE]
    if not batch:
        cursor = 0
        batch = product_list[:BATCH_SIZE]

    print(f"Total products discovered: {len(product_list)}")
    print(f"Cursor: {cursor} | Batch size: {len(batch)}")

    changes_to_post = []

    for idx, url in enumerate(batch, start=1):
        try:
            info = parse_product_page(url)
        except Exception as e:
            print(f"SKIP PRODUCT (failed): {url} -> {e}")
            continue

        old = prev.get(url)
        if old is None:
            changes_to_post.append(("NEW", info))
        elif old.get("fingerprint") != info["fingerprint"]:
            changes_to_post.append(("CHANGED", info))

        prev[url] = info

        # ✅ sleep بیشتر برای کاهش timeout
        if idx % 10 == 0:
            _sleep(PRODUCT_SLEEP_EVERY10)
        else:
            _sleep(PRODUCT_SLEEP_NORMAL)

    state["cursor"] = cursor + len(batch)
    state["products"] = prev
    save_state(state)

    if not changes_to_post:
        tg_send_message("✅ اسکن انجام شد؛ مورد جدید/تغییری در این batch پیدا نشد.")
        return

    changes_to_post = changes_to_post[:MAX_POSTS_PER_RUN]

    for kind, info in changes_to_post:
        header = "🆕 محصول جدید" if kind == "NEW" else "♻️ بروزرسانی محصول"
        model_line = f"🔹 مدل: {info['model']}\n" if info.get("model") else ""
        caption = f"{header}\n🛠 {info['title']}\n{model_line}🔗 {info['url']}{PV_LINE}"

        if info.get("img"):
            tg_send_photo(info["img"], caption)
        else:
            tg_send_message(caption)

        _sleep(TG_SEND_SLEEP)

if __name__ == "__main__":
    main()
