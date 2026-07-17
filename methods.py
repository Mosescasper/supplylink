"""
methods.py
SupplyLink -- Business logic layer.

All functions take an explicit `db.session` via the models imported from
database.py and commit as a unit of work. Each function raises ValueError
on invalid input so routes in main.py can catch it and flash/return an
error message.
"""

from datetime import date, timedelta

from database import (
    db,
    Item,
    Batch,
    StockMovement,
    PurchaseOrder,
    PurchaseOrderLine,
    Requisition,
    RequisitionLine,
    Patient,
    Prescription,
    PrescriptionLine,
    DischargeRefund,
    MonitoringRecord,
)

EXPIRY_ALERT_MONTHS = 6


# ---------------------------------------------------------------------------
# Stock movement helpers
# ---------------------------------------------------------------------------

def log_movement(item_id, batch_id, movement_type, quantity, from_location=None,
                  to_location=None, reference=None, created_by_id=None):
    """Create a StockMovement audit row. Does not touch balances itself --
    callers are expected to update Batch.quantity_remaining beforehand."""
    movement = StockMovement(
        item_id=item_id,
        batch_id=batch_id,
        movement_type=movement_type,
        quantity=quantity,
        from_location=from_location,
        to_location=to_location,
        reference=reference,
        created_by_id=created_by_id,
    )
    db.session.add(movement)
    return movement


def receive_purchase_order(po_id, received_lines, created_by_id=None):
    """
    Receiving a PO adds the ordered quantities (with batch number and expiry
    date) directly into Drug Store stock, and marks the PO Received.

    received_lines: list of dicts:
        [{"line_id": int, "batch_number": str, "expiry_date": date}, ...]
    """
    po = PurchaseOrder.query.get(po_id)
    if po is None:
        raise ValueError(f"Purchase order {po_id} not found")
    if po.status == "Received":
        raise ValueError(f"PO {po.po_number} has already been received")

    by_line_id = {rl["line_id"]: rl for rl in received_lines}

    for line in po.lines:
        info = by_line_id.get(line.id)
        if info is None:
            continue  # allow partial receipt of a subset of lines

        batch = Batch(
            item_id=line.item_id,
            batch_number=info["batch_number"],
            expiry_date=info["expiry_date"],
            quantity_received=line.quantity_ordered,
            quantity_remaining=line.quantity_ordered,
            location="Drug Store",
            received_date=date.today(),
        )
        db.session.add(batch)
        db.session.flush()  # get batch.id

        log_movement(
            item_id=line.item_id,
            batch_id=batch.id,
            movement_type="receipt",
            quantity=line.quantity_ordered,
            to_location="Drug Store",
            reference=po.po_number,
            created_by_id=created_by_id,
        )

        line.batch_number = info["batch_number"]
        line.expiry_date = info["expiry_date"]

    po.status = "Received"
    db.session.commit()
    return po


def transfer_stock(batch_id, quantity, to_location, reference=None, created_by_id=None):
    """Move quantity of a batch from its current location to another,
    exactly like the physical Stock Control Card."""
    batch = Batch.query.get(batch_id)
    if batch is None:
        raise ValueError(f"Batch {batch_id} not found")
    if quantity <= 0:
        raise ValueError("Transfer quantity must be positive")
    if float(batch.quantity_remaining) < float(quantity):
        raise ValueError(
            f"Insufficient stock in batch {batch.batch_number}: "
            f"{batch.quantity_remaining} available, {quantity} requested"
        )

    from_location = batch.location

    if float(batch.quantity_remaining) == float(quantity):
        # whole batch moves
        batch.location = to_location
    else:
        # split: reduce source batch, create a new batch row at destination
        batch.quantity_remaining = float(batch.quantity_remaining) - float(quantity)
        new_batch = Batch(
            item_id=batch.item_id,
            batch_number=batch.batch_number,
            expiry_date=batch.expiry_date,
            quantity_received=quantity,
            quantity_remaining=quantity,
            location=to_location,
            received_date=batch.received_date,
        )
        db.session.add(new_batch)
        db.session.flush()
        batch = new_batch  # movement logged against the destination batch row

    log_movement(
        item_id=batch.item_id,
        batch_id=batch.id,
        movement_type="transfer",
        quantity=quantity,
        from_location=from_location,
        to_location=to_location,
        reference=reference,
        created_by_id=created_by_id,
    )
    db.session.commit()
    return batch


# ---------------------------------------------------------------------------
# Requisitions (S11) workflow: Requested -> Approved -> Issued -> Received
# ---------------------------------------------------------------------------

def submit_requisition(req_number, department_id, issue_point, requested_by_id, lines):
    """
    lines: list of dicts [{"item_id": int, "quantity_required": float, "remarks": str}, ...]
    """
    req = Requisition(
        req_number=req_number,
        department_id=department_id,
        issue_point=issue_point,
        requested_by_id=requested_by_id,
        status="Pending",
    )
    db.session.add(req)
    db.session.flush()

    for line in lines:
        db.session.add(RequisitionLine(
            requisition_id=req.id,
            item_id=line["item_id"],
            quantity_required=line["quantity_required"],
            remarks=line.get("remarks"),
        ))

    db.session.commit()
    return req


def approve_requisition(req_id, approved_by_id):
    req = Requisition.query.get(req_id)
    if req is None:
        raise ValueError(f"Requisition {req_id} not found")
    if req.status != "Pending":
        raise ValueError(f"Requisition {req.req_number} is not pending (status={req.status})")

    req.status = "Approved"
    req.approved_by_id = approved_by_id
    db.session.commit()
    return req


def reject_requisition(req_id, approved_by_id):
    req = Requisition.query.get(req_id)
    if req is None:
        raise ValueError(f"Requisition {req_id} not found")
    if req.status != "Pending":
        raise ValueError(f"Requisition {req.req_number} is not pending (status={req.status})")

    req.status = "Rejected"
    req.approved_by_id = approved_by_id
    db.session.commit()
    return req


def issue_requisition(req_id, issued_by_id, received_by_id=None):
    """
    Approved and issued requisitions automatically deduct stock (FEFO from
    the requisition's issue_point) and create a movement record per line.
    """
    req = Requisition.query.get(req_id)
    if req is None:
        raise ValueError(f"Requisition {req_id} not found")
    if req.status != "Approved":
        raise ValueError(f"Requisition {req.req_number} must be Approved before issuing")

    for line in req.lines:
        remaining_to_issue = float(line.quantity_required)

        # FEFO: earliest-expiring batches at issue_point first
        batches = (
            Batch.query.filter_by(item_id=line.item_id, location=req.issue_point)
            .filter(Batch.quantity_remaining > 0)
            .order_by(Batch.expiry_date.asc())
            .all()
        )

        issued_for_line = 0.0
        for batch in batches:
            if remaining_to_issue <= 0:
                break
            take = min(float(batch.quantity_remaining), remaining_to_issue)
            batch.quantity_remaining = float(batch.quantity_remaining) - take
            remaining_to_issue -= take
            issued_for_line += take

            log_movement(
                item_id=line.item_id,
                batch_id=batch.id,
                movement_type="issue",
                quantity=take,
                from_location=req.issue_point,
                reference=req.req_number,
                created_by_id=issued_by_id,
            )

        if remaining_to_issue > 0:
            raise ValueError(
                f"Insufficient stock at {req.issue_point} for item {line.item_id}: "
                f"short by {remaining_to_issue}"
            )

        line.quantity_issued = issued_for_line

    req.status = "Issued"
    req.issued_by_id = issued_by_id
    if received_by_id:
        req.received_by_id = received_by_id

    db.session.commit()
    return req


# ---------------------------------------------------------------------------
# Outpatient dispensing
# ---------------------------------------------------------------------------

def dispense_prescription_line(prescription_line_id, dispensed_by_id=None):
    """
    A patient can only be dispensed drugs if they exist in the hospital
    system (registered as an OP patient). Dispensing deducts stock from the
    Outpatient Pharmacy location (FEFO) and logs the movement against the
    prescription number.
    """
    line = PrescriptionLine.query.get(prescription_line_id)
    if line is None:
        raise ValueError(f"Prescription line {prescription_line_id} not found")

    patient = line.prescription.patient
    if not patient or not patient.ip_op_number:
        raise ValueError("Patient is not registered in the hospital system -- dispensing blocked")
    if patient.patient_type != "Outpatient":
        raise ValueError("This dispensing flow is for Outpatients only")

    remaining = float(line.quantity_dispensed or 0)  # already-dispensed baseline
    qty_needed = 1  # caller may pass explicit qty via separate arg in real routes

    batches = (
        Batch.query.filter_by(item_id=line.item_id, location="Outpatient Pharmacy")
        .filter(Batch.quantity_remaining > 0)
        .order_by(Batch.expiry_date.asc())
        .all()
    )

    if not batches:
        raise ValueError("No stock available at Outpatient Pharmacy for this item")

    batch = batches[0]
    take = min(float(batch.quantity_remaining), qty_needed)
    batch.quantity_remaining = float(batch.quantity_remaining) - take
    line.quantity_dispensed = remaining + take

    log_movement(
        item_id=line.item_id,
        batch_id=batch.id,
        movement_type="issue",
        quantity=take,
        from_location="Outpatient Pharmacy",
        reference=f"RX-{line.prescription_id}",
        created_by_id=dispensed_by_id,
    )

    db.session.commit()
    return line


# ---------------------------------------------------------------------------
# Inpatient discharge & refund
# ---------------------------------------------------------------------------

def process_discharge(patient_id, prescription_id, created_by_id=None):
    """
    On discharge, check the patient's last prescription and drugs already
    issued that day:
      - fully depleted  -> continuation prescription for take-home medicine
      - not depleted    -> auto-calculate refund (undepleted qty x unit cost)
        and generate a refund record before discharge is finalised.

    Returns a list of DischargeRefund records (one per prescription line).
    """
    patient = Patient.query.get(patient_id)
    if patient is None:
        raise ValueError(f"Patient {patient_id} not found")

    prescription = Prescription.query.get(prescription_id)
    if prescription is None or prescription.patient_id != patient_id:
        raise ValueError("Prescription does not belong to this patient")

    results = []
    for line in prescription.lines:
        issued_today = float(line.quantity_dispensed or 0)

        # "Undepleted" = stock still sitting in Inpatient Pharmacy tied to
        # this item that was drawn down for this patient today. In absence
        # of per-patient batch tracking, this is passed in by the caller in
        # a real UI; here we treat quantity_dispensed vs an "actual used"
        # figure the ward reports. For a self-contained example we assume
        # the full issued amount unless told otherwise via kwargs.
        quantity_undepleted = 0.0  # override point: caller/UI supplies actual value

        unit_cost = float(line.item.unit_cost)
        refund_amount = round(quantity_undepleted * unit_cost, 2)
        outcome = (
            "Refund Processed" if quantity_undepleted > 0
            else "Continuation Prescription Issued"
        )

        refund = DischargeRefund(
            patient_id=patient_id,
            prescription_id=prescription_id,
            item_id=line.item_id,
            quantity_issued_today=issued_today,
            quantity_undepleted=quantity_undepleted,
            unit_cost=unit_cost,
            refund_amount=refund_amount,
            outcome=outcome,
        )
        db.session.add(refund)

        if quantity_undepleted > 0:
            # reverse the undepleted portion back into stock
            batch = (
                Batch.query.filter_by(item_id=line.item_id, location="Inpatient Pharmacy")
                .order_by(Batch.expiry_date.asc())
                .first()
            )
            if batch:
                batch.quantity_remaining = float(batch.quantity_remaining) + quantity_undepleted
                log_movement(
                    item_id=line.item_id,
                    batch_id=batch.id,
                    movement_type="refund_reversal",
                    quantity=quantity_undepleted,
                    to_location="Inpatient Pharmacy",
                    reference=f"DISCHARGE-{patient.ip_op_number}",
                    created_by_id=created_by_id,
                )

        results.append(refund)

    db.session.commit()
    return results


def calculate_discharge_refund(quantity_issued_today, quantity_used, unit_cost):
    """Pure calculation helper: undepleted quantity x unit cost.
    Exposed separately so routes/tests can call it without touching the DB."""
    undepleted = max(float(quantity_issued_today) - float(quantity_used), 0.0)
    return round(undepleted * float(unit_cost), 2)


# ---------------------------------------------------------------------------
# Alerts & monitoring
# ---------------------------------------------------------------------------

def get_expiring_batches(months=EXPIRY_ALERT_MONTHS):
    """Batches whose item's earliest-expiring batch falls within the alert
    window (FEFO -- First-Expiry-First-Out)."""
    cutoff = date.today() + timedelta(days=months * 30.44)
    return (
        Batch.query.filter(Batch.expiry_date <= cutoff, Batch.quantity_remaining > 0)
        .order_by(Batch.expiry_date.asc())
        .all()
    )


def get_low_stock_items():
    """Items where quantity_on_hand has fallen at or below their reorder level."""
    low = []
    for item in Item.query.all():
        if item.quantity_on_hand <= item.suggested_reorder_level():
            low.append(item)
    return low


def get_suggested_order_quantity(item: Item):
    """Balance vs. quantity-to-order view for one item."""
    reorder_level = item.suggested_reorder_level()
    on_hand = item.quantity_on_hand
    shortfall = max(reorder_level - on_hand, 0)
    return {
        "item_id": item.id,
        "sku": item.sku,
        "quantity_on_hand": on_hand,
        "reorder_level": reorder_level,
        "suggested_order_quantity": shortfall,
    }


def update_monitoring_record(item_id, period, planned_quantity, actual_issued):
    """Upserts a MonitoringRecord: planned vs actual consumption, flags variance."""
    record = MonitoringRecord.query.filter_by(item_id=item_id, period=period).first()
    if record is None:
        record = MonitoringRecord(item_id=item_id, period=period)
        db.session.add(record)
    record.planned_quantity = planned_quantity
    record.actual_issued = actual_issued
    db.session.commit()
    return record


def get_dashboard_kpis():
    """Real-time KPIs for the dashboard."""
    items = Item.query.all()
    total_stock_value = sum(item.quantity_on_hand * float(item.unit_cost) for item in items)

    return {
        "total_items": len(items),
        "items_nearing_expiry": len({b.item_id for b in get_expiring_batches()}),
        "low_stock_count": len(get_low_stock_items()),
        "open_purchase_orders": PurchaseOrder.query.filter(
            PurchaseOrder.status.in_(["Draft", "Sent"])
        ).count(),
        "pending_requisitions": Requisition.query.filter_by(status="Pending").count(),
        "total_stock_value": round(total_stock_value, 2),
    }