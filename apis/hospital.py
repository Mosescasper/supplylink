from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from extensions import db
from models import Hospital

hospital_bp = Blueprint("hospital_bp", __name__, url_prefix="/api/hospitals")


def _allowed():
    if not current_user.is_authenticated:
        return False
    return current_user.role in {"admin", "store_officer", "pharmacist"}


@hospital_bp.route("", methods=["GET"])
@login_required
def list_hospitals():
    if not _allowed():
        return jsonify({"error": "Access denied"}), 403

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


@hospital_bp.route("", methods=["POST"])
@login_required
def create_hospital():
    if not _allowed():
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    code = (data.get("code") or "").strip().upper()

    if not name or not code:
        return jsonify({"error": "Hospital name and code are required"}), 400

    if Hospital.query.filter((Hospital.name == name) | (Hospital.code == code)).first():
        return jsonify({"error": "Hospital with that name or code already exists"}), 400

    hospital = Hospital(
        name=name,
        code=code,
        address=data.get("address") or None,
        contact_person=data.get("contact_person") or None,
        phone=data.get("phone") or None,
        email=data.get("email") or None,
        is_active=data.get("is_active", True),
    )
    db.session.add(hospital)
    db.session.commit()

    return jsonify({
        "id": hospital.id,
        "name": hospital.name,
        "code": hospital.code,
        "address": hospital.address,
        "contact_person": hospital.contact_person,
        "phone": hospital.phone,
        "email": hospital.email,
        "is_active": hospital.is_active,
    }), 201


@hospital_bp.route("/<int:hospital_id>", methods=["PUT"])
@login_required
def update_hospital(hospital_id):
    if not _allowed():
        return jsonify({"error": "Access denied"}), 403

    hospital = Hospital.query.get_or_404(hospital_id)
    data = request.get_json(silent=True) or {}

    if data.get("name"):
        hospital.name = data["name"].strip()
    if data.get("code"):
        hospital.code = data["code"].strip().upper()
    if "address" in data:
        hospital.address = data.get("address") or None
    if "contact_person" in data:
        hospital.contact_person = data.get("contact_person") or None
    if "phone" in data:
        hospital.phone = data.get("phone") or None
    if "email" in data:
        hospital.email = data.get("email") or None
    if "is_active" in data:
        hospital.is_active = bool(data.get("is_active"))

    db.session.commit()
    return jsonify({
        "id": hospital.id,
        "name": hospital.name,
        "code": hospital.code,
        "address": hospital.address,
        "contact_person": hospital.contact_person,
        "phone": hospital.phone,
        "email": hospital.email,
        "is_active": hospital.is_active,
    })
