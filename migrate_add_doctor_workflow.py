"""
migrate_add_doctor_workflow.py
SupplyLink -- One-time migration adding support for doctors writing their
own prescriptions, separate from pharmacists dispensing them.

Adds:
  - prescribers.user_id           (links a Prescriber to a login account)
  - prescriptions.status          (Pending / Dispensed / Partially Dispensed)
  - prescriptions.written_by_id   (which doctor wrote it)
  - prescription_lines.quantity_prescribed  (what was asked for, vs.
                                              quantity_dispensed = what
                                              actually left the shelf)

Existing prescriptions (all created the old way, written and dispensed in
one step) are backfilled so nothing looks broken:
  - status = 'Dispensed' for every existing prescription
  - quantity_prescribed = quantity_dispensed for every existing line

No column is dropped and no existing data is touched beyond that backfill.

Usage:
    python migrate_add_doctor_workflow.py --dry-run
    python migrate_add_doctor_workflow.py
"""

import argparse

from app import app
from extensions import db


def column_exists(conn, table, column):
    result = conn.execute(db.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = :table AND column_name = :column
    """), {"table": table, "column": column}).first()
    return result is not None


def add_column(conn, dry_run, table, column, ddl_type, extra_sql=None):
    if column_exists(conn, table, column):
        print(f"  {table}.{column} already exists, skipping")
        return
    print(f"  adding {table}.{column} ({ddl_type})")
    if not dry_run:
        conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        if extra_sql:
            conn.execute(db.text(extra_sql))


def run(dry_run):
    with app.app_context():
        with db.engine.connect() as conn:
            trans = conn.begin()
            try:
                print("Step 1: prescribers.user_id")
                add_column(
                    conn, dry_run, "prescribers", "user_id", "INTEGER",
                    extra_sql=(
                        "ALTER TABLE prescribers ADD CONSTRAINT "
                        "fk_prescribers_user FOREIGN KEY (user_id) REFERENCES users(id)"
                    ),
                )
                if not dry_run:
                    exists = conn.execute(db.text("""
                        SELECT 1 FROM information_schema.table_constraints
                        WHERE table_name = 'prescribers' AND constraint_name = 'uq_prescribers_user_id'
                    """)).first()
                    if not exists:
                        conn.execute(db.text(
                            "ALTER TABLE prescribers ADD CONSTRAINT uq_prescribers_user_id UNIQUE (user_id)"
                        ))
                print()

                print("Step 2: prescriptions.status")
                add_column(conn, dry_run, "prescriptions", "status", "VARCHAR(20)")
                print("  backfilling status = 'Dispensed' for existing rows")
                if not dry_run:
                    conn.execute(db.text(
                        "UPDATE prescriptions SET status = 'Dispensed' WHERE status IS NULL"
                    ))
                    conn.execute(db.text(
                        "ALTER TABLE prescriptions ALTER COLUMN status SET NOT NULL"
                    ))
                    conn.execute(db.text(
                        "ALTER TABLE prescriptions ALTER COLUMN status SET DEFAULT 'Pending'"
                    ))
                print()

                print("Step 3: prescriptions.written_by_id")
                add_column(
                    conn, dry_run, "prescriptions", "written_by_id", "INTEGER",
                    extra_sql=(
                        "ALTER TABLE prescriptions ADD CONSTRAINT "
                        "fk_prescriptions_written_by FOREIGN KEY (written_by_id) REFERENCES users(id)"
                    ),
                )
                print()

                print("Step 4: prescription_lines.quantity_prescribed")
                add_column(conn, dry_run, "prescription_lines", "quantity_prescribed", "NUMERIC(12,2)")
                print("  backfilling quantity_prescribed = quantity_dispensed for existing rows")
                if not dry_run:
                    conn.execute(db.text(
                        "UPDATE prescription_lines SET quantity_prescribed = quantity_dispensed "
                        "WHERE quantity_prescribed IS NULL"
                    ))
                print()

                if dry_run:
                    print("-- DRY RUN: rolling back, nothing was changed --")
                    trans.rollback()
                else:
                    trans.commit()
                    print("All changes committed.")

            except Exception:
                trans.rollback()
                print("\nERROR -- transaction rolled back, no changes were made.")
                raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add doctor-writes-prescription workflow support")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing the database")
    args = parser.parse_args()
    run(args.dry_run)