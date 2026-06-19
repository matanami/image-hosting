import base64
import csv
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

SHOP_URL = "https://www.liorsub.co.il/shop/"

# הדבק כאן את הטוקן החדש שלך.
# ⚠️ אל תעלה את הסקריפט הזה ל-GitHub/repo ציבורי עם הטוקן בפנים.
GITHUB_TOKEN = "ghp_PUT_YOUR_NEW_TOKEN_HERE"

# Public repo:
# https://github.com/matanami/image-hosting
GITHUB_OWNER = "matanami"
GITHUB_REPO = "image-hosting"
GITHUB_BRANCH = "main"

# Folder inside GitHub repo
GITHUB_IMAGES_ROOT = "products-images"
GITHUB_BATCH_FOLDER = "istores-import"

# כמה מוצרים למשוך.
#   None  = כל המוצרים באתר
#   מספר  = עד כמות מסוימת (למשל 20)
PRODUCT_LIMIT = None

# הגנה: כמה עמודים מקסימום לסרוק בחנות (כדי לא להיתקע בלולאה)
MAX_SHOP_PAGES = 200

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloaded_products"
OUTPUT_CSV = BASE_DIR / "istores_products_ready.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


# ============================================================
# ISTORES CSV HEADERS — SAME STYLE AS YOUR EXAMPLE
# ============================================================

ISTORES_COLUMNS = [
    "סטטוס",
    "שם מוצר",
    "תיאור",
    "דגם",
    "מק''ט",
    "מחיר",
    "מוסתר",
    "להציג בזאפ",
    "UPC",
    "כמות במלאי",
    "כמות מינימלית בהזמנה",
    "ניהול מלאי",
    "סטטוס מלאי של המוצר",
    "סדר המוצר",
    "קטלוג פייסבוק",
    "קטגורית קטלוג",
    "תמונת מוצר ראשית",
    "קטגוריות",
    "מוצרים קשורים",
    "הנחה",
    "תאריך התחלה",
    "תאריך סיום",
    "אפשרות 1",
    "ערכים 1",
    "אפשרות 2",
    "ערכים 2",
    "אפשרות 3",
    "ערכים 3",
    "אפשרות 4",
    "ערכים 4",
]


# ============================================================
# HELPERS
# ============================================================

def clean_text(value: str) -> str:
    if not value:
        return ""

    # Remove source branding from product text
    value = value.replace("ליאור", "")
    value = value.replace("lior", "")
    value = value.replace("Lior", "")
    value = value.replace("LIOR", "")

    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_filename(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()[:90] or "product"


def clean_price(value: str) -> str:
    if not value:
        return "1"

    value = value.replace("₪", "")
    value = value.replace("ש״ח", "")
    value = value.replace('ש"ח', "")
    value = value.replace("שח", "")
    value = value.replace(",", ".")

    match = re.search(r"\d+(?:\.\d+)?", value)
    if match:
        price = match.group(0)
        if price == "0":
            return "1"
        return price

    # iStores can turn products off if price is empty/0
    return "1"


def clean_model_sku(value: str, index: int) -> str:
    return f"product-{index:03d}"


def get_soup(url: str) -> BeautifulSoup:
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def normalize_image_url(url: str) -> str:
    # WordPress resized image:
    # image-600x450.webp -> image.webp
    return re.sub(
        r"-\d+x\d+(\.(jpg|jpeg|png|webp|gif))$",
        r"\1",
        url,
        flags=re.I,
    )


# ============================================================
# SCRAPE SHOP (with pagination)
# ============================================================

def extract_product_links_from_soup(soup: BeautifulSoup) -> list[str]:
    """מחזיר את קישורי המוצרים מעמוד חנות בודד."""
    links = []
    seen = set()

    selectors = [
        "li.product a.woocommerce-LoopProduct-link",
        "li.product a.woocommerce-loop-product__link",
        "a.woocommerce-LoopProduct-link",
        "a.woocommerce-loop-product__link",
    ]

    for selector in selectors:
        for a in soup.select(selector):
            href = a.get("href", "")
            full_url = urljoin(SHOP_URL, href).split("?")[0]

            if "/product/" not in full_url:
                continue
            if full_url in seen:
                continue

            seen.add(full_url)
            links.append(full_url)

        if links:
            break

    # fallback: כל קישור שמכיל /product/
    if not links:
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            full_url = urljoin(SHOP_URL, href).split("?")[0]

            if "/product/" not in full_url:
                continue
            if full_url in seen:
                continue

            seen.add(full_url)
            links.append(full_url)

    return links


def get_product_links_from_shop() -> list[str]:
    """עובר עמוד-עמוד עד שאוסף PRODUCT_LIMIT מוצרים או שנגמרו העמודים."""
    links = []
    seen = set()
    page = 1

    while page <= MAX_SHOP_PAGES:
        if PRODUCT_LIMIT is not None and len(links) >= PRODUCT_LIMIT:
            break

        if page == 1:
            page_url = SHOP_URL
        else:
            page_url = urljoin(SHOP_URL, f"page/{page}/")

        try:
            soup = get_soup(page_url)
        except requests.HTTPError:
            # 404 = אין יותר עמודים
            print(f"   No more pages (stopped at page {page}).")
            break
        except Exception as e:
            print(f"   Failed to open shop page {page}: {e}")
            break

        page_links = extract_product_links_from_soup(soup)

        new_count = 0
        for url in page_links:
            if url not in seen:
                seen.add(url)
                links.append(url)
                new_count += 1

        print(f"   Page {page}: {new_count} new products (total {len(links)})")

        # אם לא נמצאו מוצרים חדשים - כנראה הגענו לסוף
        if new_count == 0:
            break

        page += 1
        time.sleep(1)

    return links[:PRODUCT_LIMIT]


def extract_title(soup: BeautifulSoup) -> str:
    title = soup.select_one("h1.product_title")
    if title:
        return clean_text(title.get_text(" ", strip=True))

    title = soup.select_one("h1")
    if title:
        return clean_text(title.get_text(" ", strip=True))

    return "Product"


def extract_price(soup: BeautifulSoup) -> str:
    price = soup.select_one(".summary .price, p.price, .price")
    if price:
        return clean_price(price.get_text(" ", strip=True))

    return "1"


def extract_description(soup: BeautifulSoup) -> str:
    parts = []

    short_desc = soup.select_one(".woocommerce-product-details__short-description")
    if short_desc:
        parts.append(short_desc.get_text(" ", strip=True))

    tab_desc = soup.select_one("#tab-description")
    if tab_desc:
        parts.append(tab_desc.get_text(" ", strip=True))

    description = " ".join(parts)
    return clean_text(description)


def extract_first_image_url(soup: BeautifulSoup, product_url: str) -> str | None:
    candidates = []

    for a in soup.select(".woocommerce-product-gallery__image a[href]"):
        href = a.get("href")
        if href:
            candidates.append(urljoin(product_url, href))

    for img in soup.select("img"):
        for attr in ["data-large_image", "data-src", "src"]:
            src = img.get(attr)
            if src:
                candidates.append(urljoin(product_url, src))

        srcset = img.get("srcset")
        if srcset:
            for item in srcset.split(","):
                src = item.strip().split(" ")[0]
                if src:
                    candidates.append(urljoin(product_url, src))

    seen = set()

    for img_url in candidates:
        img_url = normalize_image_url(img_url)

        if img_url in seen:
            continue

        seen.add(img_url)

        if "/wp-content/uploads/" not in img_url:
            continue

        if img_url.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return img_url

    return None


def download_image(image_url: str, target_folder: Path) -> Path:
    target_folder.mkdir(parents=True, exist_ok=True)

    response = requests.get(image_url, headers=HEADERS, timeout=60)
    response.raise_for_status()

    ext = Path(urlparse(image_url).path).suffix.lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        ext = ".jpg"

    image_path = target_folder / f"01{ext}"
    image_path.write_bytes(response.content)

    return image_path


def scrape_and_download_products() -> list[dict]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    product_links = get_product_links_from_shop()

    print(f"\nFound {len(product_links)} products to process.\n")

    products = []

    for index, product_url in enumerate(product_links, start=1):
        print(f"{index}. Scraping: {product_url}")

        try:
            soup = get_soup(product_url)
        except Exception as e:
            print(f"   Failed to open product page: {e}")
            continue

        try:
            title = extract_title(soup)
            price = extract_price(soup)
            description = extract_description(soup)
            image_url = extract_first_image_url(soup, product_url)
        except Exception as e:
            print(f"   Failed to parse product page: {e}")
            continue

        if not image_url:
            print(f"   No image found, skipping: {title}")
            continue

        folder_name = f"{index:03d} - {clean_filename(title)}"
        product_folder = DOWNLOAD_DIR / folder_name

        try:
            image_path = download_image(image_url, product_folder)
        except Exception as e:
            print(f"   Failed downloading image: {image_url}")
            print(f"   Error: {e}")
            continue

        try:
            (product_folder / "name.txt").write_text(title, encoding="utf-8")
            (product_folder / "price.txt").write_text(price, encoding="utf-8")
            (product_folder / "description.txt").write_text(description, encoding="utf-8")
            (product_folder / "url.txt").write_text(product_url, encoding="utf-8")
            (product_folder / "original_image_url.txt").write_text(image_url, encoding="utf-8")
        except Exception as e:
            print(f"   Warning: could not write meta files: {e}")

        products.append({
            "index": index,
            "title": title,
            "description": description,
            "price": price,
            "local_image": image_path,
            "source_url": product_url,
            "original_image_url": image_url,
        })

        print(f"   Title: {title}")
        print(f"   Price: {price}")
        print(f"   Image saved: {image_path}\n")

        time.sleep(1)

    return products


# ============================================================
# GITHUB UPLOAD
# ============================================================

def safe_github_folder_name(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r'[\\/:*?"<>|]', " ", value)
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")
    return value[:80] if value else "product"


def github_api_url(path_in_repo: str) -> str:
    encoded_path = quote(path_in_repo, safe="/")
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{encoded_path}"


def github_raw_url(path_in_repo: str) -> str:
    encoded_path = quote(path_in_repo, safe="/")
    return f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{encoded_path}"


def get_existing_file_sha(path_in_repo: str) -> str | None:
    url = github_api_url(path_in_repo)

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    response = requests.get(
        url,
        headers=headers,
        params={"ref": GITHUB_BRANCH},
        timeout=30,
    )

    if response.status_code == 200:
        return response.json().get("sha")

    if response.status_code == 404:
        return None

    print("GitHub check existing file failed:")
    print("URL:", url)
    print("Status:", response.status_code)
    print(response.text)
    response.raise_for_status()


def upload_file_to_github(local_file: Path, path_in_repo: str) -> str:
    url = github_api_url(path_in_repo)

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    encoded_content = base64.b64encode(local_file.read_bytes()).decode("utf-8")
    existing_sha = get_existing_file_sha(path_in_repo)

    data = {
        "message": f"Upload {path_in_repo}",
        "content": encoded_content,
        "branch": GITHUB_BRANCH,
    }

    if existing_sha:
        data["sha"] = existing_sha

    response = requests.put(url, headers=headers, json=data, timeout=60)

    if response.status_code not in (200, 201):
        print("GitHub upload failed:")
        print("Local file:", local_file)
        print("Repo path:", path_in_repo)
        print("Status:", response.status_code)
        print(response.text)
        response.raise_for_status()

    return github_raw_url(path_in_repo)


def upload_products_images_to_github(products: list[dict]) -> list[dict]:
    """מעלה תמונות. מוצר שנכשל בהעלאה מדולג ולא יופיע ב-CSV."""
    uploaded = []

    for index, product in enumerate(products, start=1):
        try:
            image_ext = product["local_image"].suffix.lower() or ".jpg"
            safe_name = safe_github_folder_name(product["title"])

            path_in_repo = (
                f"{GITHUB_IMAGES_ROOT}/"
                f"{GITHUB_BATCH_FOLDER}/"
                f"{index:03d}-{safe_name}/"
                f"01{image_ext}"
            )

            print(f"Uploading to GitHub: {product['title']}")

            image_url = upload_file_to_github(product["local_image"], path_in_repo)
            product["github_image_url"] = image_url
            uploaded.append(product)

            print(f"   GitHub raw URL: {image_url}\n")

        except Exception as e:
            print(f"   GitHub upload FAILED for '{product['title']}': {e}")
            print("   (skipping this product)\n")

        time.sleep(0.3)

    return uploaded


# ============================================================
# CSV
# ============================================================

def create_istores_csv(products: list[dict]):
    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ISTORES_COLUMNS)
        writer.writeheader()

        for row_number, product in enumerate(products, start=1):
            sku = clean_model_sku(product["title"], row_number)

            writer.writerow({
                "סטטוס": "1",
                "שם מוצר": product["title"],
                "תיאור": product["description"],
                "דגם": sku,
                "מק''ט": sku,
                "מחיר": product["price"],
                "מוסתר": "0",
                "להציג בזאפ": "0",
                "UPC": "",
                "כמות במלאי": "0",
                "כמות מינימלית בהזמנה": "1",
                "ניהול מלאי": "0",
                "סטטוס מלאי של המוצר": "קיים במלאי",
                "סדר המוצר": str(row_number),
                "קטלוג פייסבוק": "0",
                "קטגורית קטלוג": "",
                "תמונת מוצר ראשית": product["github_image_url"],
                "קטגוריות": "מוצרים",
                "מוצרים קשורים": "",
                "הנחה": "",
                "תאריך התחלה": "",
                "תאריך סיום": "",
                "אפשרות 1": "",
                "ערכים 1": "",
                "אפשרות 2": "",
                "ערכים 2": "",
                "אפשרות 3": "",
                "ערכים 3": "",
                "אפשרות 4": "",
                "ערכים 4": "",
            })


# ============================================================
# MAIN
# ============================================================

def main():
    if not GITHUB_TOKEN or GITHUB_TOKEN == "ghp_PUT_YOUR_NEW_TOKEN_HERE":
        print("Put your GitHub token in the GITHUB_TOKEN variable first.")
        return

    limit_text = "all" if PRODUCT_LIMIT is None else PRODUCT_LIMIT
    print(f"Step 1: Downloading {limit_text} products from site...\n")

    products = scrape_and_download_products()

    if not products:
        print("No products downloaded.")
        return

    print(f"Step 2: Uploading {len(products)} images to GitHub...\n")

    products = upload_products_images_to_github(products)

    if not products:
        print("No images uploaded to GitHub. Aborting CSV creation.")
        return

    print("Step 3: Creating iStores CSV...\n")

    create_istores_csv(products)

    print("Done.")
    print(f"\nProducts in CSV: {len(products)}")
    print("\nLocal product folders:")
    print(DOWNLOAD_DIR)
    print("\nFinal iStores CSV:")
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
