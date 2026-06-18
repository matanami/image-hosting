import base64
import csv
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

# Put your GitHub token here.
# IMPORTANT: do not upload this script to GitHub with the token inside.
GITHUB_TOKEN = "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"

# GitHub repo target
GITHUB_OWNER = "matanami"
GITHUB_REPO = "image-hosting"
GITHUB_BRANCH = "main"

# Folder inside the GitHub repo
GITHUB_IMAGES_ROOT = "products-images"

# Test only 3 products
PRODUCT_LIMIT = 3

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloaded_products"
OUTPUT_CSV = BASE_DIR / "github_3_products_ready.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


# ============================================================
# GENERAL HELPERS
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
        return "0"

    value = value.replace("₪", "")
    value = value.replace("ש״ח", "")
    value = value.replace('ש"ח', "")
    value = value.replace("שח", "")
    value = value.replace(",", ".")

    match = re.search(r"\d+(?:\.\d+)?", value)
    if match:
        return match.group(0)

    return "0"


def get_soup(url: str) -> BeautifulSoup:
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()

    # FIXED: using built-in parser, no lxml needed
    return BeautifulSoup(response.text, "html.parser")


def normalize_image_url(url: str) -> str:
    # WordPress sometimes gives resized images:
    # image-600x450.webp -> image.webp
    return re.sub(
        r"-\d+x\d+(\.(jpg|jpeg|png|webp|gif))$",
        r"\1",
        url,
        flags=re.I,
    )


# ============================================================
# SCRAPE SHOP
# ============================================================

def get_product_links_from_shop() -> list[str]:
    soup = get_soup(SHOP_URL)

    links = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        full_url = urljoin(SHOP_URL, href).split("?")[0]

        if "/product/" in full_url:
            links.add(full_url)

    links = sorted(links)

    if not links:
        print("No product links found on shop page.")
        return []

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

    return "0"


def extract_description(soup: BeautifulSoup) -> str:
    parts = []

    short_desc = soup.select_one(".woocommerce-product-details__short-description")
    if short_desc:
        parts.append(short_desc.get_text(" ", strip=True))

    tab_desc = soup.select_one("#tab-description")
    if tab_desc:
        parts.append(tab_desc.get_text(" ", strip=True))

    summary = soup.select_one(".summary")
    if not parts and summary:
        parts.append(summary.get_text(" ", strip=True))

    description = " ".join(parts)
    return clean_text(description)


def extract_first_image_url(soup: BeautifulSoup, product_url: str) -> str | None:
    candidates = []

    # Best WooCommerce gallery image
    for a in soup.select(".woocommerce-product-gallery__image a[href]"):
        href = a.get("href")
        if href:
            candidates.append(urljoin(product_url, href))

    # Fallback: img src / data-large_image / data-src
    for img in soup.select("img"):
        for attr in ["data-large_image", "data-src", "src"]:
            src = img.get(attr)
            if src:
                candidates.append(urljoin(product_url, src))

        # Also check srcset, choose first valid image
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

    print(f"Found {len(product_links)} products for test.")
    print()

    products = []

    for index, product_url in enumerate(product_links, start=1):
        print(f"{index}. Scraping: {product_url}")

        try:
            soup = get_soup(product_url)
        except Exception as e:
            print(f"   Failed to open product page: {e}")
            continue

        title = extract_title(soup)
        price = extract_price(soup)
        description = extract_description(soup)
        image_url = extract_first_image_url(soup, product_url)

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

        # Save local files too
        (product_folder / "name.txt").write_text(title, encoding="utf-8")
        (product_folder / "price.txt").write_text(price, encoding="utf-8")
        (product_folder / "description.txt").write_text(description, encoding="utf-8")
        (product_folder / "url.txt").write_text(product_url, encoding="utf-8")
        (product_folder / "original_image_url.txt").write_text(image_url, encoding="utf-8")

        products.append({
            "title": title,
            "description": description,
            "price": price,
            "local_image": image_path,
            "source_url": product_url,
            "original_image_url": image_url,
        })

        print(f"   Title: {title}")
        print(f"   Price: {price}")
        print(f"   Image saved: {image_path}")
        print()

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
    for index, product in enumerate(products, start=1):
        image_ext = product["local_image"].suffix.lower() or ".jpg"
        safe_name = safe_github_folder_name(product["title"])

        path_in_repo = (
            f"{GITHUB_IMAGES_ROOT}/"
            f"test-3-products/"
            f"{index:03d}-{safe_name}/"
            f"01{image_ext}"
        )

        print(f"Uploading to GitHub: {product['title']}")

        picture_url = upload_file_to_github(product["local_image"], path_in_repo)
        product["picture"] = picture_url

        print(f"   GitHub raw URL: {picture_url}")
        print()

        time.sleep(0.3)

    return products


# ============================================================
# CSV
# ============================================================

def create_csv(products: list[dict]):
    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["title", "description", "price", "picture"]
        )

        writer.writeheader()

        for product in products:
            writer.writerow({
                "title": product["title"],
                "description": product["description"],
                "price": product["price"],
                "picture": product["picture"],
            })


# ============================================================
# MAIN
# ============================================================

def main():
    if not GITHUB_TOKEN or GITHUB_TOKEN == "PUT_YOUR_GITHUB_TOKEN_HERE":
        print("Put your GitHub token in GITHUB_TOKEN first.")
        return

    print("Step 1: Downloading product data and images from site...")
    print()

    products = scrape_and_download_products()

    if not products:
        print("No products downloaded.")
        return

    print("Step 2: Uploading images to GitHub...")
    print()

    products = upload_products_images_to_github(products)

    print("Step 3: Creating CSV...")
    print()

    create_csv(products)

    print("Done.")
    print()
    print("Local product folders:")
    print(DOWNLOAD_DIR)
    print()
    print("Final CSV:")
    print(OUTPUT_CSV)
    print()
    print("Upload this CSV:")
    print("github_3_products_ready.csv")


if __name__ == "__main__":
    main()