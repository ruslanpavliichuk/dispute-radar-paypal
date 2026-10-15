"""Refresh api/catalogue.json from Channel3 (run with `make catalogue`).

A one-off snapshot: the shop reads the committed JSON and never calls Channel3 at
runtime, so the demo does not depend on their uptime or quota.
"""

import json
from pathlib import Path

from channel3_sdk import Channel3

from api.config import settings

OUT = Path(__file__).resolve().parent.parent / "api" / "catalogue.json"
QUERY = "gifts for the home and kitchen"
COUNT = 8


def slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split()[:4])


def main() -> None:
    if not settings.channel3_api_key:
        raise SystemExit("CHANNEL3_API_KEY is not set in .env")

    client = Channel3(api_key=settings.channel3_api_key)
    items = []
    for product in client.products.search(query=QUERY, limit=COUNT * 3):
        offer = next((o for o in product.offers or [] if o.price and o.price.price), None)
        if offer is None:
            continue
        image = next(
            (i.url for i in product.images or [] if i.is_main_image), None
        ) or (product.images[0].url if product.images else None)
        items.append(
            {
                "id": f"{slug(product.title)}-{product.id}".lower(),
                "title": product.title,
                "brand": product.brands[0].name if product.brands else "",
                "image_url": image,
                "price_minor": round(float(offer.price.price) * 100),
                # shortcut: demo prices are relabelled as GBP at face value (no FX); README says so
                "currency": "GBP",
            }
        )
        if len(items) == COUNT:
            break

    if len(items) < 6:
        raise SystemExit(f"only {len(items)} usable products; keeping the existing catalogue")
    OUT.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(items)} products to {OUT}")


if __name__ == "__main__":
    main()
