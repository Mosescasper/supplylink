"""
Backfills destination stock for requisitions that were issued BEFORE the
requisition_issue() destination fix. Those issues deducted stock from the
source and logged a StockMovement, but never created the corresponding
batch at the destination -- so the stock currently doesn't exist anywhere
in the system.

Handles both cases the fix covers:
  - Restock requisitions (Requisition.destination_location is set)
  - Holding-sourced requisitions raised by OP/IP Pharmacy (issue_point ==
    "Holding", destination inferred as the requesting department's name)

Reconstructs each missing destination batch from the StockMovement audit
trail (which correctly recorded batch_id and quantity at issue time), so
nothing is guessed -- it's a faithful replay of what should have happened.

USAGE (from inside your project folder, with DATABASE_URL set to the
database you want to fix):

    python backfill_restock_destinations.py S11-000002

Multiple req_numbers in one run:

    python backfill_restock_destinations.py S11-000002 S11-000005

Safe to re-run for the same req_number -- already-backfilled ones are
skipped unless you pass --force.
"""

import sys

from app import app
from extensions import db
from models import Requisition, StockMovement, Batch


def resolve_destination(req):
    """Same logic as the fixed requisition_issue() -- kept in sync so this
    backfill always matches what the live code would have done."""
    if req.destination_location:
        return req.destination_location
    elif req.issue_point == "Holding":
        return req.department.name
    return None


def backfill(req_number, force=False):
    req = Requisition.query.filter_by(req_number=req_number).first()
    if not req:
        print(f"  ! {req_number}: not found, skipping.")
        return

    destination = resolve_destination(req)
    if not destination:
        print(f"  ! {req_number}: no resolvable destination (this was genuine "
              f"consumption, not a transfer) -- nothing to backfill, skipping.")
        return

    already_marker = f"{req_number}-BACKFILLED"
    already_done = StockMovement.query.filter_by(reference=already_marker).first()
    if already_done and not force:
        print(f"  = {req_number}: already backfilled previously, skipping "
              f"(rerun with --force to redo).")
        return

    movements = StockMovement.query.filter_by(
        reference=req_number, movement_type="issue"
    ).all()

    if not movements:
        print(f"  ! {req_number}: no matching 'issue' movements found, skipping.")
        return

    restored = 0
    for m in movements:
        source_batch = Batch.query.get(m.batch_id)
        if not source_batch:
            print(f"    ! movement {m.id}: source batch no longer exists, skipping.")
            continue

        dest_batch = Batch.query.filter_by(
            item_id=m.item_id,
            batch_number=source_batch.batch_number,
            location=destination,
        ).first()

        if not dest_batch:
            dest_batch = Batch(
                item_id=m.item_id,
                batch_number=source_batch.batch_number,
                expiry_date=source_batch.expiry_date,
                quantity_received=0,
                quantity_remaining=0,
                location=destination,
            )
            db.session.add(dest_batch)
            db.session.flush()

        dest_batch.quantity_remaining = float(dest_batch.quantity_remaining) + float(m.quantity)
        restored += float(m.quantity)

    marker = StockMovement(
        item_id=movements[0].item_id,
        batch_id=movements[0].batch_id,
        movement_type="adjustment",
        quantity=0,
        from_location=None,
        to_location=destination,
        reference=already_marker,
        created_by_id=None,
    )
    db.session.add(marker)

    print(f"  + {req_number}: restored {restored:g} total units across "
          f"{len(movements)} line(s) into {destination}.")


def main():
    args = sys.argv[1:]
    force = "--force" in args
    req_numbers = [a for a in args if a != "--force"]

    if not req_numbers:
        print("Usage: python backfill_restock_destinations.py S11-000002 [S11-000005 ...] [--force]")
        sys.exit(1)

    with app.app_context():
        for req_number in req_numbers:
            backfill(req_number, force=force)
        db.session.commit()

    print("\nDone. Check the item's detail page in the app to confirm the "
          "destination quantity looks right.")


if __name__ == "__main__":
    main()