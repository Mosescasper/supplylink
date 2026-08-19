"""
reset_test_data.py

Wipes all inventory + transactional data used during testflow, so the
system can be loaded fresh with real institutional data.

KEPT (untouched):
    - Users
    - Departments
    - Hospitals
    - Suppliers
    - Categories

WIPED (in dependency-safe order, children before parents):
    - DischargeRefund
    - PrescriptionLine, Prescription
    - PatientDocument (+ files on disk), PatientFile, PatientMovement,
      PatientNote, Appointment, Patient
    - Prescriber   <-- see note below, easy to skip if you want to keep these
    - RequisitionLine, Requisition
    - PurchaseRequisitionLine, PurchaseRequisition
    - DeliveryLineItem, Delivery
    - PurchaseOrderLine, PurchaseOrder
    - SupplierItem (catalog entries pointing at items being deleted)
    - ProcurementPlan, MonitoringRecord
    - StockMovement
    - Batch
    - Item

NOTE on Prescriber: these are just a lookup/autocomplete convenience layer.
Doctor-linked prescribers (user_id set) are auto-recreated the first time
that doctor writes a new prescription, so wiping them loses nothing real.
If you'd rather KEEP existing Prescriber rows, comment out the
`Prescriber.query.delete()` line near the bottom of wipe().

USAGE:
    1. BACK UP FIRST:
         pg_dump -U your_user -d your_dbname -F c -f afya_link_backup_before_reset.dump

    2. Run this script:
         python reset_test_data.py

    3. It will show you row counts and require you to type RESET to confirm.

    4. Afterwards, re-run your stocktake import to load fresh item/batch data:
         python import_stocktake.py "path\\to\\your_new_stocktake.xlsx"
"""

import os
import sys

from app import app
from extensions import db
from models import (
    DischargeRefund,
    PrescriptionLine, Prescription,
    PatientDocument, PatientFile, PatientMovement, PatientNote,
    Appointment, Patient,
    Prescriber,
    RequisitionLine, Requisition,
    PurchaseRequisitionLine, PurchaseRequisition,
    DeliveryLineItem, Delivery,
    PurchaseOrderLine, PurchaseOrder,
    SupplierItem,
    ProcurementPlan, MonitoringRecord,
    StockMovement,
    Batch,
    Item,
)

# Adjust this if your uploaded patient documents live somewhere else.
PATIENT_DOCS_DIR = "uploads/patient_documents"

MODELS_IN_DELETE_ORDER = [
    DischargeRefund,
    PrescriptionLine, Prescription,
    PatientDocument, PatientFile, PatientMovement, PatientNote,
    Appointment, Patient,
    Prescriber,  # <-- comment this line out if you want to KEEP prescriber lookups
    RequisitionLine, Requisition,
    PurchaseRequisitionLine, PurchaseRequisition,
    DeliveryLineItem, Delivery,
    PurchaseOrderLine, PurchaseOrder,
    SupplierItem,
    ProcurementPlan, MonitoringRecord,
    StockMovement,
    Batch,
    Item,
]


def show_counts():
    print("\nCurrent row counts (models about to be wiped):")
    for model in MODELS_IN_DELETE_ORDER:
        count = model.query.count()
        print(f"  {model.__tablename__:<30} {count}")
    print("\nKept untouched: users, departments, hospitals, suppliers, categories\n")


def delete_patient_document_files():
    """Remove uploaded patient document files from disk before the DB rows
    referencing them are gone. Non-fatal if a file is already missing."""
    docs = PatientDocument.query.all()
    if not docs:
        return
    print(f"Deleting {len(docs)} patient document file(s) from disk...")
    for doc in docs:
        path = os.path.join(PATIENT_DOCS_DIR, doc.stored_filename)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            print(f"  WARNING: could not delete {path}: {e}")


def wipe():
    with app.app_context():
        show_counts()

        confirm = input(
            "Type RESET (all caps) to permanently delete the data above, "
            "or anything else to cancel: "
        )
        if confirm != "RESET":
            print("Cancelled. Nothing was deleted.")
            sys.exit(0)

        delete_patient_document_files()

        total_deleted = 0
        for model in MODELS_IN_DELETE_ORDER:
            deleted = model.query.delete(synchronize_session=False)
            total_deleted += deleted
            print(f"  Deleted {deleted} row(s) from {model.__tablename__}")

        db.session.commit()

        print(f"\nDone. {total_deleted} total row(s) deleted.")
        print("Users, Departments, Hospitals, Suppliers, and Categories were left untouched.")
        print("\nNext step: re-run your stocktake import to load fresh items/batches, e.g.")
        print('  python import_stocktake.py "path\\to\\your_new_stocktake.xlsx"')


if __name__ == "__main__":
    wipe()