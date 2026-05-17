import requests
from bs4 import BeautifulSoup
import csv
import re
import time
import os
import sys
import logging
from datetime import datetime

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
OUTPUT_CSV      = "jaquar_products.csv"
SKU_FILE        = "skus.txt"          # one SKU per line
BATCH_SIZE      = int(os.environ.get("BATCH_SIZE_OVERRIDE", 500))  # overridable via env
SLEEP_BETWEEN   = 2                   # seconds between requests
REQUEST_TIMEOUT = 8
MAX_RETRIES     = 1                   # per-SKU retry attempts
RETRY_BACKOFF   = 2                   # seconds added per retry

# ------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------
# Colour mapping
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
# Checkpoint helpers
# ------------------------------------------------------------
CSV_FIELDS = ["sku", "product_name", "image_url", "error"]

def load_completed_skus(output_csv):
    done = set()
    if not os.path.exists(output_csv):
        return done
    with open(output_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Only skip if it actually succeeded (has image_url, no error)
            if row.get("sku") and row.get("image_url") and not row.get("error"):
                done.add(row["sku"].strip())
    log.info(f"Checkpoint: {len(done)} SKUs already completed, skipping them.")
    return done

def append_result(result, output_csv):
    """Append a single result row to the CSV (create with header if new)."""
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
    """Load SKUs from a text file (one per line) or fall back to inline list."""
    if os.path.exists(sku_file):
        with open(sku_file, encoding="utf-8") as f:
            skus = [line.strip() for line in f if line.strip()]
        log.info(f"Loaded {len(skus)} SKUs from {sku_file}")
        return skus
    # Fallback inline list (replace with your actual SKUs or keep empty)
    log.warning(f"{sku_file} not found – using inline fallback list.")
    return [
        "LAG-CHR-91011BWF",
        "LAG-BLM-91011BWF",
        "JSA-NAW-DLX9022",
    ]

def get_batch(skus, batch_index, batch_size):
    """Return the slice for a given batch index (0-based). None = all."""
    if not batch_size:
        return skus
    start = batch_index * batch_size
    end   = start + batch_size
    return skus[start:end]

# ------------------------------------------------------------
# Width extraction
# ------------------------------------------------------------
def extract_width_from_sku(sku):
    parts = re.findall(r'\d+', sku)
    for p in parts:
        w = int(p)
        if 100 <= w <= 3000:
            return w
    return None

# ------------------------------------------------------------
# Core scraper (single SKU, with retries)
# ------------------------------------------------------------
def scrape_product(sku, session):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = _scrape_once(sku, session)
            if result["error"] is None:
                return result
            # Scrape logic returned a soft error – don't retry those
            return result
        except Exception as e:
            last_error = str(e)
            wait = RETRY_BACKOFF * attempt
            log.warning(f"  Attempt {attempt}/{MAX_RETRIES} failed for {sku}: {e}. Retrying in {wait}s…")
            time.sleep(wait)

    return {"sku": sku, "product_name": None, "image_url": None,
            "error": f"All {MAX_RETRIES} attempts failed: {last_error}"}


def _scrape_once(sku, session):
    result = {"sku": sku, "product_name": None, "image_url": None, "error": None}

    # Step 1: search
    search_url = f"https://www.jaquar.com/en/search?q={sku}"
    resp = session.get(search_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)

    # Step 2: resolve to product page if not already redirected
    if "?Id=" not in resp.url:
        soup = BeautifulSoup(resp.text, "html.parser")
        first_prod = soup.find("a", class_="product-name") or \
                     soup.find("a", href=re.compile(r"\?Id=\d+"))
        if not first_prod:
            result["error"] = "No product found in search results"
            return result
        product_url = first_prod["href"]
        if not product_url.startswith("http"):
            product_url = "https://www.jaquar.com" + product_url
        resp = session.get(product_url, timeout=REQUEST_TIMEOUT)

    soup = BeautifulSoup(resp.text, "html.parser")

    # Step 3: product ID
    product_div = soup.find("div", class_="product-detail-inner")
    if not product_div:
        result["error"] = "Could not find product-detail-inner div"
        return result
    product_id = product_div.get("data-productid")
    if not product_id:
        result["error"] = "Missing data-productid"
        return result

    # Step 4: product name
    name_tag = soup.find("h1", id=lambda x: x and x.startswith("product-name-"))
    result["product_name"] = (
        name_tag.get_text(strip=True) if name_tag
        else (soup.title.string.strip() if soup.title else "Unknown")
    )

    # Step 5: anti-forgery token
    token_input = soup.find("input", {"name": "__RequestVerificationToken"})
    token = token_input.get("value") if token_input else None
    if not token:
        result["error"] = "Missing anti-forgery token"
        return result

    # Step 6: attribute variants or static image
    attr_section = soup.find("div", class_="attributes")
    image_url = None

    if attr_section and attr_section.find("input", type="radio"):
        attribute_ul = attr_section.find("ul")
        if not attribute_ul:
            result["error"] = "Attribute list not found"
            return result

        is_colour   = attribute_ul.get("id", "").startswith("image-squares")
        attribute_id = attribute_ul.get("data-attr")

        if is_colour:
            parts = sku.split("-")
            colour_code = parts[1] if len(parts) > 1 else None
            if not colour_code or colour_code not in COLOUR_SKU_TO_TITLE:
                result["error"] = f"Unknown colour code in SKU: {colour_code}"
                return result
            target_title = COLOUR_SKU_TO_TITLE[colour_code]

            colour_map = {}
            for li in attribute_ul.find_all("li"):
                inp = li.find("input")
                if inp and inp.get("title") and inp.get("value"):
                    colour_map[inp["title"]] = inp["value"]

            if target_title not in colour_map:
                result["error"] = f"Colour '{target_title}' not found on page"
                return result

            post_data = {
                f"product_attribute_{attribute_id}": colour_map[target_title],
                "__RequestVerificationToken": token,
            }

        else:
            width = extract_width_from_sku(sku)
            if width is None:
                result["error"] = "Cannot extract width from SKU"
                return result

            size_map = {}
            for li in attribute_ul.find_all("li"):
                inp = li.find("input")
                lbl = li.find("label")
                if inp and lbl:
                    size_map[lbl.get_text(strip=True)] = inp.get("value")

            matched_value = None
            for label, val in size_map.items():
                m = re.search(r'W:\s*(\d+)\s*-\s*(\d+)', label)
                if m and int(m.group(1)) <= width <= int(m.group(2)):
                    matched_value = val
                    break

            if matched_value is None:
                result["error"] = f"Width {width} not matched to any size range"
                return result

            post_data = {
                f"product_attribute_{attribute_id}": matched_value,
                "__RequestVerificationToken": token,
            }

        change_url = (
            f"https://www.jaquar.com/en/shoppingcart/productdetails_attributechange"
            f"?productId={product_id}&validateAttributeConditions=False&loadPicture=True"
        )
        resp_attr = session.post(change_url, data=post_data, timeout=REQUEST_TIMEOUT)
        data = resp_attr.json()
        image_url = data.get("pictureDefaultSizeUrl")
        if not image_url:
            result["error"] = "No image URL in attribute change response"
            return result

    else:
        img_tag = soup.find("img", class_="container-image")
        if img_tag and img_tag.get("src"):
            image_url = img_tag["src"]
        else:
            result["error"] = "No image found on static page"
            return result

    if image_url and not image_url.startswith("http"):
        image_url = "https://www.jaquar.com" + image_url

    result["image_url"] = image_url
    return result

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
if __name__ == "__main__":
    # Optional CLI args: python scrape.py <batch_index>
    # e.g. python scrape.py 0   → first 500
    #      python scrape.py 1   → next 500, etc.
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
            log.info(f"  ✓ {sku} → {result['product_name']}")

        append_result(result, OUTPUT_CSV)   # written immediately – crash-safe
        time.sleep(SLEEP_BETWEEN)

    elapsed = datetime.now() - start_time
    log.info(f"Batch {batch_index} complete in {elapsed}. Results in {OUTPUT_CSV}")
