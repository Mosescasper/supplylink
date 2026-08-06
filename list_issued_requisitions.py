"""
Lists every Issued requisition, so you can decide which ones need backfilling
with backfill_restock_destinations.py.

Flags each one as:
  RESTOCK    -- destination_location is set (Drug Store -> Holding, etc.)
  PHARMACY   -- issue_point == "Holding" (OP/IP Pharmacy requisitions)
  CONSUMPTION -- everything else (genuine department consumption -- these
                 never needed a destination batch, nothing to backfill)

USAGE (from inside your project folder, with DATABASE_URL set):

    python list_issued_requisitions.py
"""

from app import app
from models import Requisition


def classify(req):
    if req.destination_location:
        return "RESTOCK", req.destination_location
    elif req.issue_point == "Holding":
        return "PHARMACY", req.department.name
    return "CONSUMPTION", None


def main():
    with app.app_context():
        reqs = (
            Requisition.query.filter_by(status="Issued")
            .order_by(Requisition.created_at.asc())
            .all()
        )

        if not reqs:
            print("No Issued requisitions found.")
            return

        needs_backfill = []
        print(f"{'REQ NUMBER':<14} {'ISSUE POINT':<20} {'TYPE':<12} {'DESTINATION':<20} DATE")
        print("-" * 90)
        for req in reqs:
            req_type, destination = classify(req)
            print(f"{req.req_number:<14} {req.issue_point:<20} {req_type:<12} "
                  f"{(destination or '-'):<20} {req.created_at.strftime('%Y-%m-%d')}")
            if req_type in ("RESTOCK", "PHARMACY"):
                needs_backfill.append(req.req_number)

        print("-" * 90)
        print(f"\n{len(reqs)} Issued requisition(s) total.")
        print(f"{len(needs_backfill)} likely need backfilling (RESTOCK / PHARMACY types).")

        if needs_backfill:
            print("\nCopy-paste this to backfill all of them in one run:\n")
            print("python backfill_restock_destinations.py " + " ".join(needs_backfill))


if __name__ == "__main__":
    main()