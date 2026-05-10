"""Тесты модуля формирования отчётов."""

import pytest
from datetime import datetime, timedelta

from configs import config as cfg
from modules.db import Database
from modules.reports import ReportGenerator, ReportPeriod


@pytest.fixture()
def populated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.DB, "sqlite_path", str(tmp_path / "rep.db"))
    monkeypatch.setattr(cfg.DB, "backend", "sqlite")
    d = Database(backend="sqlite")
    d.init_schema()

    # 2 заказа, 3 дефекта (workpiece не используется)
    pt = d.fetch_one("SELECT profile_type_id FROM profile_type WHERE code='ALFA'")
    pt2 = d.fetch_one("SELECT profile_type_id FROM profile_type WHERE code='BEAM'")
    o1 = d.insert_returning_id(
        "production_order",
        {"order_code": "R-1",
         "profile_type_id": pt["profile_type_id"],
         "target_length_mm": 1500, "target_angle_deg": 90, "quantity": 2},
        "order_id",
    )
    o2 = d.insert_returning_id(
        "production_order",
        {"order_code": "R-2",
         "profile_type_id": pt2["profile_type_id"],
         "target_length_mm": 2000, "target_angle_deg": 45, "quantity": 1},
        "order_id",
    )
    edge = d.fetch_one("SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'")
    scratch = d.fetch_one("SELECT defect_type_id FROM defect_type WHERE code='SCRATCH'")
    for order_id, idx, dt_id, st in [
        (o1, 1, edge["defect_type_id"], "q4"),
        (o1, 2, scratch["defect_type_id"], "q3"),
        (o2, 1, edge["defect_type_id"], "q6"),
    ]:
        d.insert_returning_id(
            "defect",
            {"order_id": order_id, "unit_index": idx,
             "defect_type_id": dt_id,
             "detected_state": st, "measured_value": 2.0,
             "target_value": 1500.0,
             "threshold_value": 1.0, "auto_detected": 1},
            "defect_id",
        )
    yield d
    d.close()


def test_csv_generation(populated_db, tmp_path):
    rep = ReportGenerator(populated_db, out_dir=str(tmp_path))
    period = ReportPeriod(period_from=datetime.utcnow() - timedelta(hours=1),
                          period_to=datetime.utcnow() + timedelta(hours=1))
    path = rep.generate_csv(period)
    assert path.exists()
    txt = path.read_text(encoding="utf-8-sig")
    assert "EDGE_DEFORMATION" in txt
    assert "SCRATCH" in txt


def test_xlsx_generation(populated_db, tmp_path):
    rep = ReportGenerator(populated_db, out_dir=str(tmp_path))
    period = ReportPeriod(period_from=datetime.utcnow() - timedelta(hours=1),
                          period_to=datetime.utcnow() + timedelta(hours=1))
    path = rep.generate_xlsx(period)
    assert path.exists()
    assert path.stat().st_size > 1000


def test_pdf_generation(populated_db, tmp_path):
    rep = ReportGenerator(populated_db, out_dir=str(tmp_path))
    period = ReportPeriod(period_from=datetime.utcnow() - timedelta(hours=1),
                          period_to=datetime.utcnow() + timedelta(hours=1))
    path = rep.generate_pdf(period)
    assert path.exists()
    assert path.stat().st_size > 2000


def test_report_registered_in_db(populated_db, tmp_path):
    rep = ReportGenerator(populated_db, out_dir=str(tmp_path))
    period = ReportPeriod(period_from=datetime.utcnow() - timedelta(hours=1),
                          period_to=datetime.utcnow() + timedelta(hours=1))
    rep.generate_pdf(period)
    rows = populated_db.fetch_all("SELECT format, total_defects FROM defect_report")
    assert len(rows) == 1
    assert rows[0]["format"] == "PDF"
    assert rows[0]["total_defects"] == 3
