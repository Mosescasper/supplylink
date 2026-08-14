"""
Import script: loads MONTHLY_CONSUMPTION_PROJECTION.xlsx into the Item
catalog only (no stock/batches -- staff enter real batches as stock
physically arrives, since this file has no batch numbers or expiry dates).

Usage (run from the project root, venv active, on whichever machine/database
you want this data loaded into):

    python import_formulary.py path/to/MONTHLY_CONSUMPTION_PROJECTION.xlsx

Safe to re-run: any SKU that already exists in the database is skipped, not
duplicated or overwritten, so running this twice (or against a database that
already has some of these items) won't create duplicates.
"""

import sys
import openpyxl

from app import app, db
from models import Item

SHEET_NAME = "CONSU RATE "
FIRST_DATA_ROW = 6
LAST_DATA_ROW = 381


def parse_rows(xlsx_path):
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb[SHEET_NAME]

    rows = list(ws.iter_rows(min_row=FIRST_DATA_ROW, max_row=LAST_DATA_ROW, values_only=True))

    items = []
    auto_counter = 0

    for r in rows:
        # Columns: (blank, S/NO, ITEM CODE, CATEGORY AND NAME, PHARMACEUTICALS FORM,
        #           UNIT OF ISSUE, MONTHLY CONSUMPTION PROJECTION, PHY STOCK, blank)
        _, _sno, item_code, cat_name, form, unit_of_issue, monthly_proj, _phy_stock, _ = r

        if not cat_name and not item_code:
            continue  # fully blank row

        if item_code:
            sku = str(item_code).strip()
        else:
            auto_counter += 1
            sku = f"AUTO-{auto_counter:03d}"

        name_parts = [str(cat_name).strip()] if cat_name else []
        if form:
            name_parts.append(str(form).strip())
        name = " — ".join(name_parts)

        unit = str(unit_of_issue).strip() if unit_of_issue else "Units"
        reorder_level = float(monthly_proj) if monthly_proj else 0

        items.append({
            "sku": sku,
            "name": name,
            "unit_of_issue": unit,
            "reorder_level": reorder_level,
        })

    return items


def run(xlsx_path):
    items = parse_rows(xlsx_path)
    print(f"Parsed {len(items)} items from {xlsx_path}")

    with app.app_context():
        existing_skus = {sku for (sku,) in db.session.query(Item.sku).all()}

        created = 0
        skipped = 0

        for it in items:
            if it["sku"] in existing_skus:
                skipped += 1
                continue

            db.session.add(Item(
                sku=it["sku"],
                name=it["name"],
                unit_of_issue=it["unit_of_issue"],
                unit_cost=0,
                reorder_level=it["reorder_level"],
            ))
            created += 1

        db.session.commit()
        print(f"Created {created} new items.")
        print(f"Skipped {skipped} items (SKU already existed).")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python import_formulary.py path/to/MONTHLY_CONSUMPTION_PROJECTION.xlsx")
        sys.exit(1)

    run(sys.argv[1])