import os, re, json, time, hashlib
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_LIST_URL = os.getenv("RONIX_LIST_URL", "https://www.ronix.ir/power-tools/")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHANNEL_ID = os.getenv("TG_CHANNEL_ID")  # @ir_ronix

PRICE_BOT = "@SsAaSsHhAaRr_bot"
PV_LINE = f"\n💬 برای استعلام قیمت به ربات پیام بدین\n{PRICE_BOT}"

if not BOT_TOKEN or not CHANNEL_ID:
    raise SystemExit("Missing TG_BOT_TOKEN or TG_CHANNEL_ID (set as GitHub Secrets).")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; RonixWatcher/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.7,en;q=0.6",
    "Connection": "keep-alive",
})

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict) and "products" in data:
                    return data
            except json.JSONDecodeError:
                pass
    return {"products": {}}

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

# ✅ مقاوم‌سازی: Timeout بیشتر + Retry + Backoff
def fetch(url: str) -> str:
    last_err = None
    for attempt in range(1, 4):  # 3 tries
        try:
            r = session.get(url, timeout=90)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            time.sleep(2 * attempt)  # 2s, 4s, 6s
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

        # ✅ اگر لیست صفحه هم خطا داد، کل برنامه نخوابه
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

        time.sleep(1.2)

    return sorted(product_urls)

def main():
    state = load_state()
    prev = state.get("products", {})
    first_run = (len(prev) == 0)

    product_list = crawl_all_products(BASE_LIST_URL)

    changes_to_post = []

    for idx, url in enumerate(product_list, start=1):
        # ✅ اگر یک محصول لود نشد، کل اجرا Fail نشه
        try:
            info = parse_product_page(url)
        except Exception as e:
            print(f"SKIP PRODUCT (failed): {url} -> {e}")
            continue

        old = prev.get(url)

        if first_run:
            changes_to_post.append(("NEW", info))
        else:
            if old is None:
                changes_to_post.append(("NEW", info))
            elif old.get("fingerprint") != info["fingerprint"]:
                changes_to_post.append(("CHANGED", info))

        prev[url] = info

        time.sleep(1.2 if idx % 10 else 2.0)

    state["products"] = prev
    save_state(state)

    if not changes_to_post:
        tg_send_message("✅ اسکن روزانه انجام شد؛ تغییری پیدا نشد.")
        return

    max_posts = int(os.getenv("MAX_POSTS_PER_RUN", "40"))
    changes_to_post = changes_to_post[:max_posts]

    for kind, info in changes_to_post:
        header = "🆕 محصول جدید" if kind == "NEW" else "♻️ بروزرسانی محصول"
        model_line = f"🔹 مدل: {info['model']}\n" if info.get("model") else ""
        caption = f"{header}\n🛠 {info['title']}\n{model_line}🔗 {info['url']}{PV_LINE}"

        if info.get("img"):
            tg_send_photo(info["img"], caption)
        else:
            tg_send_message(caption)

        time.sleep(1.5)

if __name__ == "__main__":
    main()
