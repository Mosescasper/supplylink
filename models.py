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


class User(UserMixin, db.Model):
    __tablename__ = "users"

    # Only three roles exist in the system now.
    ROLES = ("admin", "store_officer", "pharmacist")

    # Human-friendly labels for dropdowns / templates.
    ROLE_LABELS = {
        "admin": "Admin",
        "store_officer": "Store Officer",
        "pharmacist": "Pharmacist",
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


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class Item(db.Model):
    __tablename__ = "items"

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(50), nullable=False, unique=True)
    name = db.Column(db.String(200), nullable=False)
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
# Patients, prescriptions, discharge
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


class Prescription(db.Model):
    __tablename__ = "prescriptions"

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    date = db.Column(db.Date, default=date.today)
    prescriber_name = db.Column(db.String(150), nullable=False)
    registration_number = db.Column(db.String(50))
    designation = db.Column(db.String(100))

    patient = db.relationship("Patient", back_populates="prescriptions")
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