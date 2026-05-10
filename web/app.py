"""Flask-приложение для оператора и начальника участка.

Возможности:
  * /                    — дашборд (счётчики дефектов, последние заказы);
  * /orders              — список производственных заказов;
  * /orders/new          — оператор создаёт новый заказ;
  * /orders/<id>         — карточка заказа со всеми зафиксированными дефектами;
  * /defect/manual       — оператор / контролёр регистрирует дефект вручную;
  * /report              — начальник участка выбирает период и формат отчёта,
                            нажимает кнопку и получает готовый файл (PDF / XLSX
                            / CSV) на скачивание.

Подключается к той же базе данных, что и остальные модули прототипа,
поэтому веб-интерфейс не требует отдельного запуска CodeSys / OPC UA.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from modules.db import Database
from modules.defect_control import DefectController
from modules.reports import ReportGenerator, ReportPeriod


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.secret_key = "digital-twin-prototype"

    def _db() -> Database:
        db = Database()
        db.init_schema()
        return db

    # -------------------------------------------------- Дашборд

    @app.route("/")
    def dashboard():
        db = _db()
        try:
            stats = db.fetch_all(
                """
                SELECT dt.code, dt.name_ru, COUNT(d.defect_id) AS cnt
                FROM defect_type dt
                LEFT JOIN defect d ON d.defect_type_id = dt.defect_type_id
                GROUP BY dt.code, dt.name_ru
                ORDER BY cnt DESC, dt.code
                """
            )
            orders = db.fetch_all(
                """
                SELECT o.order_id, o.order_code, pt.code AS profile,
                       o.target_length_mm, o.target_angle_deg, o.quantity,
                       o.status, o.created_at,
                       (SELECT COUNT(*) FROM defect d WHERE d.order_id=o.order_id) AS defect_cnt
                FROM production_order o
                JOIN profile_type pt ON pt.profile_type_id = o.profile_type_id
                ORDER BY o.created_at DESC
                LIMIT 10
                """
            )
            total = sum(int(r["cnt"]) for r in stats)
            return render_template(
                "dashboard.html",
                stats=stats,
                orders=orders,
                total_defects=total,
            )
        finally:
            db.close()

    # -------------------------------------------------- Список заказов

    @app.route("/orders")
    def orders_list():
        db = _db()
        try:
            orders = db.fetch_all(
                """
                SELECT o.order_id, o.order_code, pt.code AS profile,
                       o.target_length_mm, o.target_angle_deg, o.quantity,
                       o.color, o.needs_milling, o.status, o.created_at,
                       (SELECT COUNT(*) FROM defect d WHERE d.order_id=o.order_id) AS defect_cnt
                FROM production_order o
                JOIN profile_type pt ON pt.profile_type_id = o.profile_type_id
                ORDER BY o.created_at DESC
                """
            )
            return render_template("orders_list.html", orders=orders)
        finally:
            db.close()

    # -------------------------------------------------- Новый заказ

    @app.route("/orders/new", methods=["GET", "POST"])
    def order_new():
        db = _db()
        try:
            profiles = db.fetch_all(
                "SELECT profile_type_id, code, name_ru, cut_correction_mm "
                "FROM profile_type ORDER BY code"
            )
            if request.method == "POST":
                form = request.form
                try:
                    db.execute(
                        """
                        INSERT INTO production_order
                            (order_code, profile_type_id, target_length_mm,
                             target_angle_deg, quantity, needs_milling, color, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'NEW')
                        """,
                        (
                            form["order_code"],
                            int(form["profile_type_id"]),
                            int(form["target_length_mm"]),
                            float(form.get("target_angle_deg") or 90.0),
                            int(form["quantity"]),
                            1 if form.get("needs_milling") else 0,
                            form.get("color") or None,
                        ),
                    )
                    flash(f"Заказ {form['order_code']} создан", "success")
                    return redirect(url_for("orders_list"))
                except Exception as e:  # noqa: BLE001
                    flash(f"Ошибка: {e}", "error")
            return render_template("order_new.html", profiles=profiles)
        finally:
            db.close()

    # -------------------------------------------------- Карточка заказа

    @app.route("/orders/<int:order_id>")
    def order_detail(order_id: int):
        db = _db()
        try:
            order = db.fetch_one(
                """
                SELECT o.*, pt.code AS profile, pt.name_ru AS profile_name,
                       pt.cut_correction_mm
                FROM production_order o
                JOIN profile_type pt ON pt.profile_type_id = o.profile_type_id
                WHERE o.order_id = %s
                """,
                (order_id,),
            )
            if not order:
                flash("Заказ не найден", "error")
                return redirect(url_for("orders_list"))
            defects = db.fetch_all(
                """
                SELECT d.defect_id, d.unit_index, d.detected_at, d.detected_state,
                       d.measured_value, d.target_value, d.threshold_value,
                       d.auto_detected, d.comment,
                       dt.code AS defect_code, dt.name_ru AS defect_name,
                       e.code AS equipment_code
                FROM defect d
                JOIN defect_type dt ON dt.defect_type_id = d.defect_type_id
                LEFT JOIN equipment e ON e.equipment_id = d.equipment_id
                WHERE d.order_id = %s
                ORDER BY d.detected_at DESC
                """,
                (order_id,),
            )
            defect_types = db.fetch_all(
                "SELECT defect_type_id, code, name_ru FROM defect_type ORDER BY code"
            )
            equipment = db.fetch_all(
                "SELECT equipment_id, code, name_ru FROM equipment ORDER BY code"
            )
            return render_template(
                "order_detail.html",
                order=order,
                defects=defects,
                defect_types=defect_types,
                equipment=equipment,
            )
        finally:
            db.close()

    # -------------------------------------------------- Ручная регистрация дефекта

    @app.route("/defect/manual", methods=["POST"])
    def defect_manual():
        db = _db()
        try:
            controller = DefectController(db)
            form = request.form
            order_id = int(form["order_id"])
            controller.register_manual(
                order_id=order_id,
                unit_index=int(form.get("unit_index") or 1),
                defect_code=form["defect_code"],
                detected_state=form.get("detected_state") or "q4",
                equipment_id=int(form["equipment_id"]) if form.get("equipment_id") else None,
                comment=form.get("comment") or "",
            )
            flash("Дефект зарегистрирован", "success")
            return redirect(url_for("order_detail", order_id=order_id))
        finally:
            db.close()

    # -------------------------------------------------- Отчёт

    @app.route("/report", methods=["GET", "POST"])
    def report():
        if request.method == "POST":
            db = _db()
            try:
                form = request.form
                period_from = datetime.fromisoformat(form["period_from"])
                period_to = datetime.fromisoformat(form["period_to"])
                period = ReportPeriod(period_from=period_from, period_to=period_to)
                rep = ReportGenerator(db)
                fmt = form.get("format", "PDF").upper()
                if fmt == "XLSX":
                    path = rep.generate_xlsx(period)
                elif fmt == "CSV":
                    path = rep.generate_csv(period)
                else:
                    path = rep.generate_pdf(period)
                return send_file(str(path), as_attachment=True)
            finally:
                db.close()
        # GET — форма с дефолтными датами (последние 7 дней)
        now = datetime.utcnow()
        return render_template(
            "report.html",
            default_from=(now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"),
            default_to=now.strftime("%Y-%m-%dT%H:%M"),
        )

    return app


if __name__ == "__main__":  # pragma: no cover
    create_app().run(debug=False, port=5000)
