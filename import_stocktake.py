"""
Import the June 2026 Drug Store Stocktake into SupplyLink / Afya Link.

WHAT THIS DOES
---------------
- Reads stocktake_data.json (236 items parsed from your uploaded stocktake
  report: SKU, item name, category, unit of issue, unit cost, physical
  quantity, and expiry date where available).
- Creates a Category ("Pharmaceuticals" or "Nutrition Consumables") if it
  doesn't already exist -- it should already, since you seeded 30
  categories earlier.
- Creates an Item for each row (skipped if that SKU already exists, so
  this script is safe to re-run without creating duplicates).
- For rows that had a parseable expiry date, also creates a Batch with
  that item's physical quantity, located in "Drug Store" (since this is
  the Drug Store stocktake report), and logs a matching "receipt"
  StockMovement so it shows up in your audit trail / reports correctly.
- 4 rows had no expiry date on the report ("N/A") -- those items are
  still created, but as catalog-only entries with no batch. You can
  receive stock into them later via Batches -> Receive Stock once you
  know a real expiry date.

HOW TO RUN
----------
1. Place this file AND stocktake_data.json in your project root
   (same folder as app.py).
2. From your activated venv:
       python import_stocktake.py
3. Review the summary it prints. It is safe to run more than once --
   anything already imported (matched by SKU) will be skipped, not
   duplicated.

WHAT TO CHECK AFTERWARDS
-------------------------
- Go to /inventory and confirm items appear with sensible names/costs.
- Go to /batches and filter by "Drug Store" to see the new stock.
- Check /expiry-alerts and /dashboard as admin/store_officer to confirm
  the near-expiry and low-stock alerts are now populated with real data,
  since your database previously had zero items.
"""

import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app  # noqa: E402
from extensions import db  # noqa: E402
from models import Item, Batch, Category, StockMovement  # noqa: E402

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stocktake_data.json")
IMPORT_LOCATION = "Drug Store"
IMPORT_REFERENCE = "Stocktake Import - June 2026"


def get_or_create_category(name):
    cat = Category.query.filter_by(name=name.strip()).first()
    if cat:
        return cat
    cat = Category(name=name.strip())
    db.session.add(cat)
    db.session.flush()
    print(f"  [new category] {name}")
    return cat


def run_import():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        rows = json.load(f)

    created_items = 0
    skipped_items = 0
    created_batches = 0
    catalog_only = 0

    category_cache = {}

    for row in rows:
        sku = row["sku"]
        existing = Item.query.filter_by(sku=sku).first()
        if existing:
            skipped_items += 1
            continue

        cat_name = row["category"]
        if cat_name not in category_cache:
            category_cache[cat_name] = get_or_create_category(cat_name)
        category = category_cache[cat_name]

        item = Item(
            sku=sku,
            name=row["name"],
            category_id=category.id,
            unit_of_issue=row["unit"] or "Units",
            unit_cost=row["unit_cost"] or 0,
            reorder_level=0,  # not present in the stocktake report; set manually per item later
        )
        db.session.add(item)
        db.session.flush()
        created_items += 1

        if row["expiry"] and row["qty"] and row["qty"] > 0:
            expiry_date = datetime.strptime(row["expiry"], "%Y-%m-%d").date()
            batch = Batch(
                item_id=item.id,
                batch_number=f"STOCKTAKE-JUN2026-{sku}",
                expiry_date=expiry_date,
                quantity_received=row["qty"],
                quantity_remaining=row["qty"],
                location=IMPORT_LOCATION,
            )
            db.session.add(batch)
            db.session.flush()

            movement = StockMovement(
                item_id=item.id,
                batch_id=batch.id,
                movement_type="receipt",
                quantity=row["qty"],
                to_location=IMPORT_LOCATION,
                reference=IMPORT_REFERENCE,
            )
            db.session.add(movement)
            created_batches += 1
        else:
            catalog_only += 1

    db.session.commit()

    print()
    print("=" * 60)
    print("IMPORT COMPLETE")
    print("=" * 60)
    print(f"Items created:        {created_items}")
    print(f"Items skipped (dupe): {skipped_items}")
    print(f"Batches created:      {created_batches}")
    print(f"Catalog-only (no expiry on report): {catalog_only}")
    print(f"Total items in file:  {len(rows)}")


if __name__ == "__main__":
    with app.app_context():
        run_import()