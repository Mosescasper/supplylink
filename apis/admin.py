from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import db
from models import Department, User, Hospital

admin_bp = Blueprint("admin_bp", __name__, url_prefix="/api/admin")


def _admin_required():
    if not current_user.is_authenticated:
        return False
    return current_user.role == "admin"


@admin_bp.route("/users", methods=["GET"])
@login_required
def list_users():
    if not _admin_required():
        return jsonify({"error": "Admin access required"}), 403

    users = User.query.order_by(User.name).all()
    return jsonify([
        {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "department_id": user.department_id,
            "is_active": user.is_active_flag,
        }
        for user in users
    ])


@admin_bp.route("/users", methods=["POST"])
@login_required
def create_user():
    if not _admin_required():
        return jsonify({"error": "Admin access required"}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = (data.get("role") or "store_officer").strip()
    department_id = data.get("department_id") or None

    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are required"}), 400

    if role not in User.ROLES:
        return jsonify({"error": "Invalid role"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email already registered"}), 400

    user = User(name=name, email=email, role=role, department_id=department_id)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    return jsonify({"id": user.id, "name": user.name, "email": user.email, "role": user.role}), 201


@admin_bp.route("/departments", methods=["GET"])
@login_required
def list_departments():
    if not _admin_required():
        return jsonify({"error": "Admin access required"}), 403

    departments = Department.query.order_by(Department.name).all()
    return jsonify([
        {"id": dept.id, "name": dept.name, "is_store": dept.is_store}
        for dept in departments
    ])


@admin_bp.route("/departments", methods=["POST"])
@login_required
def create_department():
    if not _admin_required():
        return jsonify({"error": "Admin access required"}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Department name is required"}), 400

    if Department.query.filter_by(name=name).first():
        return jsonify({"error": "Department already exists"}), 400

    dept = Department(name=name, is_store=data.get("is_store", False))
    db.session.add(dept)
    db.session.commit()

    return jsonify({"id": dept.id, "name": dept.name, "is_store": dept.is_store}), 201


@admin_bp.route("/hospitals", methods=["GET"])
@login_required
def list_hospitals():
    if not _admin_required():
        return jsonify({"error": "Admin access required"}), 403

    hospitals = Hospital.query.order_by(Hospital.name).all()
    return jsonify([
        {
            "id": hospital.id,
            "name": hospital.name,
            "code": hospital.code,
            "address": hospital.address,
            "contact_person": hospital.contact_person,
            "phone": hospital.phone,
            "email": hospital.email,
            "is_active": hospital.is_active,
        }
        for hospital in hospitals
    ])
