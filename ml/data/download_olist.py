"""Download the Olist dataset into ml/data/raw/olist/ (gitignored).

    uv run python -m ml.data.download_olist

If the download fails, put the Kaggle zip at ml/data/raw/olist.zip and run again.
"""
import shutil
import sys
import zipfile
from pathlib import Path

import httpx

from api.config import settings

URL = "https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce"
RAW = Path(__file__).parent / "raw"
OUT = RAW / "olist"
ZIP = RAW / "olist.zip"
MAX_CSV_BYTES = 500 * 1024 * 1024  # the real files are under 70 MB
EXPECTED = {
    "olist_orders_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_reviews_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
    "olist_customers_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_geolocation_dataset.csv",
    "product_category_name_translation.csv",
}


def present() -> bool:
    return all((OUT / name).exists() for name in EXPECTED)


def download() -> None:
    key = settings.kaggle_api_key
    if not key:
        sys.exit(f"KAGGLE_API_KEY is not set. Download the zip manually to {ZIP} and rerun.")
    if key.startswith("KGAT_"):
        auth, headers = None, {"Authorization": f"Bearer {key}"}
    elif settings.kaggle_username:
        auth, headers = (settings.kaggle_username, key), {}
    else:
        sys.exit("A legacy Kaggle key needs KAGGLE_USERNAME too, or download the zip manually.")
    RAW.mkdir(parents=True, exist_ok=True)
    tmp = ZIP.with_suffix(".part")
    try:
        with httpx.stream("GET", URL, auth=auth, headers=headers, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        sys.exit(f"Kaggle download failed ({type(exc).__name__}). Download the zip manually to {ZIP} and rerun.")
    tmp.replace(ZIP)


def extract() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        z = zipfile.ZipFile(ZIP)
    except zipfile.BadZipFile:
        ZIP.unlink()
        sys.exit(f"{ZIP} was not a valid zip and was removed. Rerun, or download it manually.")
    with z:
        for info in z.infolist():
            # only the known flat CSV names; anything else (paths, "..") is ignored
            if info.filename not in EXPECTED:
                continue
            if info.file_size > MAX_CSV_BYTES:
                sys.exit(f"{info.filename} is {info.file_size:,} bytes, over the {MAX_CSV_BYTES:,} cap")
            tmp = OUT / (info.filename + ".part")  # atomic: present() never sees half a file
            with z.open(info) as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            tmp.replace(OUT / info.filename)


def main() -> None:
    if present():
        print(f"Olist already in {OUT}")
        return
    if not ZIP.exists():
        download()
    extract()
    missing = EXPECTED - {p.name for p in OUT.iterdir()}
    if missing:
        sys.exit(f"Zip is missing: {sorted(missing)}")
    print(f"Olist ready in {OUT}")


if __name__ == "__main__":
    main()
