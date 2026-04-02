# blueprints/auth.py
from flask import Blueprint, request, redirect, url_for, session, render_template, jsonify
from extensions import db
from models import UserCRM

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET"])
def login_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("auth/login.html")


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.form
    correo = (data.get("correo") or "").strip().lower()
    password = data.get("password") or ""

    user = UserCRM.query.filter(
        db.func.lower(UserCRM.correo) == correo,
        UserCRM.activo.is_(True),
    ).first()

    if not user or not user.check_password(password):
        return render_template("auth/login.html", error="Correo o contraseña incorrectos")

    session.permanent = True
    session["user_id"] = str(user.id)
    session["user_nombre"] = user.nombre
    session["user_correo"] = user.correo
    session["user_rol"] = user.rol.value

    return redirect(url_for("index"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))


@auth_bp.route("/api/me")
def me():
    if not session.get("user_id"):
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({
        "id": session["user_id"],
        "nombre": session["user_nombre"],
        "correo": session["user_correo"],
        "rol": session["user_rol"],
    })
