import requests
from bs4 import BeautifulSoup
import csv, re, time, os, sys, logging
from datetime import datetime

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
OUTPUT_CSV      = "artize_essco_products.csv"
SKU_FILE        = "skus_pending.txt"
BATCH_SIZE      = int(os.environ.get("BATCH_SIZE_OVERRIDE", 500))
SLEEP_BETWEEN   = 3                   # slightly higher than jaquar - these sites are slower
REQUEST_TIMEOUT = 8
MAX_RETRIES     = 1
RETRY_BACKOFF   = 2

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("artize_essco_scraper.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------
# Colour mapping (identical to Jaquar scraper)
# ------------------------------------------------------------
COLOUR_SKU_TO_TITLE = {
    "CHR": "Chrome",
    "BCH": "Black Chrome",
    "BLM": "Black Matt",
    "BGP": "Blush Gold Bright PVD",
    "GBP": "Gold Bright PVD",
    "ACR": "Antique Copper",
    "ABR": "Antique Bronze",
    "GMP": "Gold Matt PVD",
    "GRF": "Graphite",
    "BGM": "Lever: Gold Matt PVD | Body: Black Matt",
    "BBC": "Lever: Black Chrome | Body: Black Matt",
    "GMG": "Lever: Gold Bright PVD | Body: Gold Matt PVD",
}

# ------------------------------------------------------------
# CSV
# ------------------------------------------------------------
CSV_FIELDS = ["sku", "product_name", "image_url", "source", "error"]

def load_completed_skus(output_csv):
    """Only skip SKUs that succeeded (have image_url, no error)."""
    done = set()
    if not os.path.exists(output_csv):
        return done
    with open(output_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("sku") and row.get("image_url") and not row.get("error"):
                done.add(row["sku"].strip())
    log.info(f"Checkpoint: {len(done)} SKUs already completed, skipping them.")
    return done

def append_result(result, output_csv):
    file_exists = os.path.exists(output_csv)
    with open(output_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: result.get(k) for k in CSV_FIELDS})

# ------------------------------------------------------------
# SKU helpers
# ------------------------------------------------------------
def load_skus(sku_file):
    if os.path.exists(sku_file):
        with open(sku_file, encoding="utf-8") as f:
            skus = [line.strip() for line in f if line.strip()]
        log.info(f"Loaded {len(skus)} SKUs from {sku_file}")
        return skus
    log.warning(f"{sku_file} not found.")
    return []

def get_batch(skus, batch_index, batch_size):
    if not batch_size:
        return skus
    start = batch_index * batch_size
    return skus[start:start + batch_size]

# ------------------------------------------------------------
# Artize scraper
# ------------------------------------------------------------
def scrape_artize(sku, session):
    result = {"sku": sku, "product_name": None, "image_url": None,
              "source": "artize", "error": None}

    # Step 1: search
    search_url = (
        f"https://www.artize.com/in/index.php"
        f"?route=product/search&language=en-gb&search={sku}"
    )
    try:
        resp = session.get(search_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        result["error"] = f"Artize request failed: {e}"
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Step 2: find product page link from search results
    product_link = None

    # Try common OpenCart search result selectors
    for selector in [
        "a.product-thumb",
        "div.product-thumb a",
        "h4.product-name a",
        "a[href*='product_id']",
    ]:
        tag = soup.select_one(selector)
        if tag and tag.get("href"):
            product_link = tag["href"]
            break

    if not product_link:
        result["error"] = "Artize: No product found in search results"
        return result

    if not product_link.startswith("http"):
        product_link = "https://www.artize.com" + product_link

    # Step 3: fetch product page
    try:
        resp = session.get(product_link, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        result["error"] = f"Artize product page failed: {e}"
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Step 4: product name
    name_tag = soup.select_one("h1.product-title")
    result["product_name"] = name_tag.get_text(strip=True) if name_tag else "Unknown"

    # Step 5: check for colour swatches
    swatches = soup.select("li.option-image")

    if swatches:
        colour_code = sku.split("-")[1] if len(sku.split("-")) > 1 else None
        if not colour_code or colour_code not in COLOUR_SKU_TO_TITLE:
            result["error"] = f"Artize: Unknown colour code in SKU: {colour_code}"
            return result

        target_finish = COLOUR_SKU_TO_TITLE[colour_code]
        image_url = None
        for li in swatches:
            if li.get("option-name") == target_finish:
                image_url = li.get("option_orig_image")
                break

        if not image_url:
            result["error"] = f"Artize: Colour '{target_finish}' not found in swatches"
            return result

        result["image_url"] = image_url

    else:
        # Static product - grab main image
        img_tag = soup.select_one("a#section3_img1")
        if img_tag and img_tag.get("href"):
            result["image_url"] = img_tag["href"]
        else:
            # fallback: any large product image
            img_tag = soup.select_one(".product-image a")
            if img_tag and img_tag.get("href"):
                result["image_url"] = img_tag["href"]
            else:
                result["error"] = "Artize: No image found on product page"
                return result

    return result

# ------------------------------------------------------------
# Essco scraper
# ------------------------------------------------------------
def scrape_essco(sku, session):
    result = {"sku": sku, "product_name": None, "image_url": None,
              "source": "essco", "error": None}

    # Essco redirects directly to product page
    search_url = (
        f"https://www.esscobathware.com/index.php"
        f"?route=product/search&language=en-gb&search={sku}"
    )
    try:
        resp = session.get(search_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        result["error"] = f"Essco request failed: {e}"
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Check if we landed on a product page or search results
    # Product page has .product-thumbnail-link; search page has .product-thumb
    thumbnail = soup.select_one("a.product-thumbnail-link[data-index='0']")

    if not thumbnail:
        # Maybe it showed search results - try to follow first link
        first_link = soup.select_one("div.product-thumb a, h4.product-name a")
        if not first_link:
            result["error"] = "Essco: No product found"
            return result

        product_url = first_link["href"]
        if not product_url.startswith("http"):
            product_url = "https://www.esscobathware.com" + product_url

        try:
            resp = session.get(product_url, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            result["error"] = f"Essco product page failed: {e}"
            return result

        soup = BeautifulSoup(resp.text, "html.parser")
        thumbnail = soup.select_one("a.product-thumbnail-link[data-index='0']")

    if not thumbnail or not thumbnail.get("href"):
        result["error"] = "Essco: No image found on product page"
        return result

    # Product name
    name_tag = soup.select_one("h1.signle-product-title, h1.single-product-title, h1.product-title")
    result["product_name"] = name_tag.get_text(strip=True) if name_tag else "Unknown"

    # Image - use data-index=0 (first/main image), href is the full size
    image_url = thumbnail["href"]
    if not image_url.startswith("http"):
        image_url = "https://www.esscobathware.com" + image_url

    result["image_url"] = image_url
    return result

# ------------------------------------------------------------
# Main scraper: try Artize then Essco
# ------------------------------------------------------------
def scrape_product(sku, session):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Try Artize first
            result = scrape_artize(sku, session)
            if not result["error"]:
                return result

            artize_error = result["error"]
            log.info(f"  Artize failed ({artize_error}), trying Essco...")

            # Try Essco
            result = scrape_essco(sku, session)
            if not result["error"]:
                return result

            # Both failed - return combined error
            result["error"] = f"Artize: {artize_error} | Essco: {result['error']}"
            return result

        except Exception as e:
            last_error = str(e)
            wait = RETRY_BACKOFF * attempt
            log.warning(f"  Attempt {attempt}/{MAX_RETRIES} exception: {e}. Retrying in {wait}s…")
            time.sleep(wait)

    return {"sku": sku, "product_name": None, "image_url": None,
            "source": None, "error": f"All attempts failed: {last_error}"}

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
if __name__ == "__main__":
    batch_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    all_skus  = load_skus(SKU_FILE)
    batch     = get_batch(all_skus, batch_index, BATCH_SIZE)
    done_skus = load_completed_skus(OUTPUT_CSV)
    pending   = [s for s in batch if s not in done_skus]

    total   = len(batch)
    skipped = total - len(pending)
    log.info(f"Batch {batch_index}: {total} SKUs | {skipped} already done | {len(pending)} to scrape")

    if not pending:
        log.info("Nothing to do – all SKUs in this batch are already completed.")
        sys.exit(0)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
    })

    start_time = datetime.now()
    for i, sku in enumerate(pending, 1):
        log.info(f"[{i}/{len(pending)}] Scraping: {sku}")
        result = scrape_product(sku, session)

        if result["error"]:
            log.warning(f"  ✗ {sku} → {result['error']}")
        else:
            log.info(f"  ✓ {sku} → {result['product_name']} [{result['source']}]")

        append_result(result, OUTPUT_CSV)
        time.sleep(SLEEP_BETWEEN)

    elapsed = datetime.now() - start_time
    log.info(f"Batch {batch_index} complete in {elapsed}. Results in {OUTPUT_CSV}")
