"""
One-time backfill: any item with NO batch at Drug Store yet (i.e. the 235
items that came in with zero stock during the formulary import) gets a
placeholder batch created there -- quantity 0, so it doesn't affect stock
value or reporting, but it makes the item show up in Drug Store's scoped
inventory list and dashboard count, ready for real stock to be received
into later.

Batch number is tied to the item's SKU (e.g. "PENDING-SA001") rather than
truly random, so it stays traceable/searchable later:

    SELECT * FROM batches WHERE batch_number LIKE 'PENDING-%';

Expiry date uses the same far-future placeholder (2099-12-31) as the
earlier import, for the same reason -- it should never trigger a false
"near expiry" alert.

Usage (run from the project root, venv active):

    python backfill_zero_stock_batches.py

Safe to re-run -- only creates a batch for an item if it truly has none at
Drug Store yet, so running it twice won't create duplicates.
"""

from datetime import date

from app import app, db
from models import Item, Batch

PLACEHOLDER_EXPIRY = date(2099, 12, 31)
LOCATION = "Drug Store"


def run():
    with app.app_context():
        items = Item.query.all()

        items_with_batch_here = {
            b.item_id for b in Batch.query.filter_by(location=LOCATION).all()
        }

        created = 0
        for item in items:
            if item.id in items_with_batch_here:
                continue

            db.session.add(Batch(
                item_id=item.id,
                batch_number=f"PENDING-{item.sku}",
                expiry_date=PLACEHOLDER_EXPIRY,
                quantity_received=0,
                quantity_remaining=0,
                location=LOCATION,
            ))
            created += 1

        db.session.commit()
        print(f"Created {created} placeholder batches at {LOCATION}.")
        print(f"({len(items) - created} items already had a batch there.)")


if __name__ == "__main__":
    run()