from datetime import datetime, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db


# ---------------------------------------------------------------------------
# Core reference tables
# ---------------------------------------------------------------------------

class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    is_store = db.Column(db.Boolean, default=False)  # Drug Store, Holding, Pharmacy points

    users = db.relationship("User", back_populates="department")

    def __repr__(self):
        return f"<Department {self.name}>"


class Hospital(db.Model):
    __tablename__ = "hospitals"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True, index=True)
    code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    address = db.Column(db.String(255))
    contact_person = db.Column(db.String(150))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(150))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Hospital {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    # Four roles exist in the system now.
    ROLES = ("admin", "store_officer", "pharmacist", "doctor")

    # Human-friendly labels for dropdowns / templates.
    ROLE_LABELS = {
        "admin": "Admin",
        "store_officer": "Store Officer",
        "pharmacist": "Pharmacist",
        "doctor": "Doctor",
    }

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default="store_officer")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"))
    is_active_flag = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    department = db.relationship("Department", back_populates="users")

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_active(self):
        return self.is_active_flag

    @property
    def role_label(self):
        return self.ROLE_LABELS.get(self.role, self.role)

    def has_role(self, *roles):
        return self.role in roles

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)

    items = db.relationship("Item", back_populates="category")


class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    contact_person = db.Column(db.String(150))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(150))
    address = db.Column(db.String(255))

    items = db.relationship("Item", back_populates="supplier")
    purchase_orders = db.relationship("PurchaseOrder", back_populates="supplier")
    deliveries = db.relationship("Delivery", back_populates="supplier")
    catalog_items = db.relationship("SupplierItem", back_populates="supplier",
                                     cascade="all, delete-orphan",
                                     order_by="SupplierItem.id")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class Item(db.Model):
    __tablename__ = "items"

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), nullable=False, unique=True)
    name = db.Column(db.String(500), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    unit_of_issue = db.Column(db.String(50), nullable=False, default="Units")
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    avg_monthly_consumption = db.Column(db.Numeric(12, 2), default=0)
    reorder_level = db.Column(db.Numeric(12, 2), default=0)
    reorder_level_manual_override = db.Column(db.Boolean, default=False)

    category = db.relationship("Category", back_populates="items")
    supplier = db.relationship("Supplier", back_populates="items")
    batches = db.relationship("Batch", back_populates="item")

    @property
    def quantity_on_hand(self):
        return sum((b.quantity_remaining or 0) for b in self.batches)

    @property
    def quantity_on_hand_by_location(self):
        totals = {}
        for b in self.batches:
            totals[b.location] = totals.get(b.location, 0) + (b.quantity_remaining or 0)
        return totals

    @property
    def is_low_stock(self):
        return self.quantity_on_hand <= (self.reorder_level or 0)

    @property
    def suggested_order_quantity(self):
        deficit = (self.reorder_level or 0) - self.quantity_on_hand
        return max(deficit, 0)

    @property
    def earliest_expiring_batch(self):
        active = [b for b in self.batches if (b.quantity_remaining or 0) > 0 and b.expiry_date]
        return min(active, key=lambda b: b.expiry_date) if active else None

    def __repr__(self):
        return f"<Item {self.sku} {self.name}>"


LOCATIONS = ("Drug Store", "Holding", "Outpatient Pharmacy", "Inpatient Pharmacy")


class Batch(db.Model):
    __tablename__ = "batches"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    batch_number = db.Column(db.String(100), nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    quantity_received = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    quantity_remaining = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    location = db.Column(db.String(50), nullable=False, default="Drug Store")
    received_date = db.Column(db.Date, default=date.today)

    item = db.relationship("Item", back_populates="batches")
    movements = db.relationship("StockMovement", back_populates="batch")

    def is_near_expiry(self, months=6):
        if not self.expiry_date:
            return False
        delta_days = (self.expiry_date - date.today()).days
        return 0 <= delta_days <= (months * 30)

    def __repr__(self):
        return f"<Batch {self.batch_number} exp {self.expiry_date}>"


class StockMovement(db.Model):
    __tablename__ = "stock_movements"

    MOVEMENT_TYPES = ("receipt", "issue", "transfer", "adjustment", "refund_reversal")

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    movement_type = db.Column(db.String(30), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    from_location = db.Column(db.String(50))
    to_location = db.Column(db.String(50))
    reference = db.Column(db.String(100))  # S11 no. / PO no. / prescription no. / delivery note
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship("Item")
    batch = db.relationship("Batch", back_populates="movements")
    created_by = db.relationship("User")


# ---------------------------------------------------------------------------
# Procurement
# ---------------------------------------------------------------------------

class PurchaseOrder(db.Model):
    __tablename__ = "purchase_orders"

    STATUSES = ("Draft", "Sent", "Received", "Cancelled")

    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(50), nullable=False, unique=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Draft")
    order_date = db.Column(db.Date, default=date.today)
    expected_date = db.Column(db.Date)

    supplier = db.relationship("Supplier", back_populates="purchase_orders")
    lines = db.relationship("PurchaseOrderLine", back_populates="purchase_order",
                             cascade="all, delete-orphan")
    deliveries = db.relationship("Delivery", back_populates="purchase_order")

    @property
    def total_value(self):
        return sum((l.quantity_ordered or 0) * (l.unit_cost or 0) for l in self.lines)


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_ordered = db.Column(db.Numeric(12, 2), nullable=False)
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    quantity_received = db.Column(db.Numeric(12, 2), default=0)

    purchase_order = db.relationship("PurchaseOrder", back_populates="lines")
    item = db.relationship("Item")


class Delivery(db.Model):
    """
    A real-world delivery event: what a supplier actually dropped off, when,
    and at what price. Always linked to the Purchase Order it fulfils.
    """
    __tablename__ = "deliveries"

    STATUSES = ("Received", "Partially Received", "Rejected")

    id = db.Column(db.Integer, primary_key=True)
    delivery_number = db.Column(db.String(50), nullable=False, unique=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    delivery_note_number = db.Column(db.String(100))  # supplier's own invoice/delivery note ref
    delivery_date = db.Column(db.Date, default=date.today)
    received_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Received")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    purchase_order = db.relationship("PurchaseOrder", back_populates="deliveries")
    supplier = db.relationship("Supplier", back_populates="deliveries")
    received_by = db.relationship("User")
    lines = db.relationship("DeliveryLineItem", back_populates="delivery",
                             cascade="all, delete-orphan")

    @property
    def total_value(self):
        return sum((l.quantity_delivered or 0) * (l.unit_price or 0) for l in self.lines)

    def __repr__(self):
        return f"<Delivery {self.delivery_number} for {self.purchase_order.po_number}>"


class DeliveryLineItem(db.Model):
    """One item delivered as part of a Delivery: quantity, batch, expiry,
    and the price actually charged this time (may differ from the item's
    standard unit_cost)."""
    __tablename__ = "delivery_line_items"

    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.Integer, db.ForeignKey("deliveries.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    batch_number = db.Column(db.String(100), nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    quantity_delivered = db.Column(db.Numeric(12, 2), nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    delivery = db.relationship("Delivery", back_populates="lines")
    item = db.relationship("Item")

    @property
    def line_total(self):
        return (self.quantity_delivered or 0) * (self.unit_price or 0)


class SupplierItem(db.Model):
    """An item a supplier is known to stock, with the price/lead-time they
    typically quote. Informational catalog entry — not tied to any actual
    order or delivery, just a reference used when raising POs."""
    __tablename__ = "supplier_items"

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    typical_unit_cost = db.Column(db.Numeric(12, 2))
    lead_time_days = db.Column(db.Integer)
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    supplier = db.relationship("Supplier", back_populates="catalog_items")
    item = db.relationship("Item")

    __table_args__ = (
        db.UniqueConstraint("supplier_id", "item_id", name="uq_supplier_item"),
    )

    def __repr__(self):
        return f"<SupplierItem supplier={self.supplier_id} item={self.item_id}>"


class ProcurementPlan(db.Model):
    __tablename__ = "procurement_plans"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    planned_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    planned_monthly_breakdown = db.Column(db.JSON)  # {"1": qty, "2": qty, ...}

    item = db.relationship("Item")


class MonitoringRecord(db.Model):
    __tablename__ = "monitoring_records"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    period = db.Column(db.String(20), nullable=False)  # e.g. "2026-07"
    planned_quantity = db.Column(db.Numeric(12, 2), default=0)
    actual_issued = db.Column(db.Numeric(12, 2), default=0)

    item = db.relationship("Item")

    @property
    def variance(self):
        return (self.planned_quantity or 0) - (self.actual_issued or 0)


# ---------------------------------------------------------------------------
# Requisitions (S11)
# ---------------------------------------------------------------------------

class Requisition(db.Model):
    __tablename__ = "requisitions"

    STATUSES = ("Pending", "Approved", "Rejected", "Issued")

    id = db.Column(db.Integer, primary_key=True)
    req_number = db.Column(db.String(50), nullable=False, unique=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    issue_point = db.Column(db.String(50), nullable=False)  # Drug Store / Holding
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Pending")
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    received_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    department = db.relationship("Department")
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    issued_by = db.relationship("User", foreign_keys=[issued_by_id])
    received_by = db.relationship("User", foreign_keys=[received_by_id])
    lines = db.relationship("RequisitionLine", back_populates="requisition",
                             cascade="all, delete-orphan")


class RequisitionLine(db.Model):
    __tablename__ = "requisition_lines"

    id = db.Column(db.Integer, primary_key=True)
    requisition_id = db.Column(db.Integer, db.ForeignKey("requisitions.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_required = db.Column(db.Numeric(12, 2), nullable=False)
    quantity_issued = db.Column(db.Numeric(12, 2), default=0)
    remarks = db.Column(db.String(255))

    requisition = db.relationship("Requisition", back_populates="lines")
    item = db.relationship("Item")

    @property
    def value(self):
        return (self.quantity_issued or 0) * (self.item.unit_cost if self.item else 0)


# ---------------------------------------------------------------------------
# Patients, prescribers, prescriptions, discharge
# ---------------------------------------------------------------------------

class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    gender = db.Column(db.String(10))
    ip_op_number = db.Column(db.String(50), nullable=False, unique=True)
    patient_type = db.Column(db.String(20), nullable=False, default="Outpatient")
    age = db.Column(db.Integer)
    weight = db.Column(db.Numeric(6, 2))
    height = db.Column(db.Numeric(6, 2))
    contact = db.Column(db.String(30))
    clinic_ward_unit = db.Column(db.String(100))
    drug_allergies = db.Column(db.Text)

    prescriptions = db.relationship("Prescription", back_populates="patient")


class Prescriber(db.Model):
    """A doctor/clinician who can be looked up by typing a few letters of
    their name at the point of dispensing, instead of typing the full name,
    registration number, and designation every time.

    Prescription still stores its own snapshot columns (prescriber_name,
    registration_number, designation) at the moment the prescription is
    written — prescriber_id just links back to the reusable record so those
    snapshot fields can be auto-filled rather than retyped.

    user_id links this record back to a logged-in doctor's own account, for
    the ones auto-created the first time that doctor writes a prescription
    (see _get_or_create_own_prescriber in app.py). Prescribers created by a
    pharmacist/admin for an external doctor who has no login of their own
    are left with user_id=None."""
    __tablename__ = "prescribers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, index=True)
    registration_number = db.Column(db.String(50))
    designation = db.Column(db.String(100))
    phone = db.Column(db.String(30))
    is_active = db.Column(db.Boolean, default=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

    def __repr__(self):
        return f"<Prescriber {self.name}>"


class Prescription(db.Model):
    __tablename__ = "prescriptions"

    STATUSES = ("Pending", "Partially Dispensed", "Dispensed")

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    prescriber_id = db.Column(db.Integer, db.ForeignKey("prescribers.id"))
    # The doctor's own User account, when a prescription was written through
    # the doctor login flow (prescription_new). NULL for the pharmacist's
    # walk-in/phoned-in dispense routes, which write and dispense in one
    # step without going through a doctor account.
    written_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    date = db.Column(db.Date, default=date.today)
    prescriber_name = db.Column(db.String(150), nullable=False)
    registration_number = db.Column(db.String(50))
    designation = db.Column(db.String(100))
    # "Pending" (doctor wrote it, nothing dispensed yet), "Partially
    # Dispensed", or "Dispensed" (fully given out). Walk-in dispense routes
    # set this straight to "Dispensed" since they write and dispense at once.
    status = db.Column(db.String(30), nullable=False, default="Pending")

    patient = db.relationship("Patient", back_populates="prescriptions")
    prescriber = db.relationship("Prescriber")
    written_by = db.relationship("User", foreign_keys=[written_by_id])
    lines = db.relationship("PrescriptionLine", back_populates="prescription",
                             cascade="all, delete-orphan")


class PrescriptionLine(db.Model):
    __tablename__ = "prescription_lines"

    id = db.Column(db.Integer, primary_key=True)
    prescription_id = db.Column(db.Integer, db.ForeignKey("prescriptions.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    medicine_name = db.Column(db.String(200), nullable=False)  # INN
    dose = db.Column(db.String(50))
    route = db.Column(db.String(50))
    frequency = db.Column(db.String(50))
    duration = db.Column(db.String(50))
    # What the doctor prescribed. For the walk-in dispense routes (which
    # write and dispense in the same step) this is set equal to whatever
    # was actually dispensed, so remaining-quantity math stays correct.
    quantity_prescribed = db.Column(db.Numeric(12, 2), default=0)
    quantity_dispensed = db.Column(db.Numeric(12, 2), default=0)
    dispensed = db.Column(db.Boolean, default=False)

    prescription = db.relationship("Prescription", back_populates="lines")
    item = db.relationship("Item")


class DischargeRefund(db.Model):
    __tablename__ = "discharge_refunds"

    OUTCOMES = ("Continuation Prescription Issued", "Refund Processed")

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    prescription_id = db.Column(db.Integer, db.ForeignKey("prescriptions.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    quantity_issued_today = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    quantity_undepleted = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    refund_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    outcome = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship("Patient")
    prescription = db.relationship("Prescription")
    item = db.relationship("Item")