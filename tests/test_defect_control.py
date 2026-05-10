"""Тесты модуля контроля и выявления дефектов."""

import pytest

from configs import config as cfg
from modules.db import Database
from modules.defect_control import DefectController


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.DB, "sqlite_path", str(tmp_path / "ctrl.db"))
    monkeypatch.setattr(cfg.DB, "backend", "sqlite")
    d = Database(backend="sqlite")
    d.init_schema()
    pt = d.fetch_one("SELECT profile_type_id FROM profile_type WHERE code='ALFA'")
    d.insert_returning_id(
        "production_order",
        {"order_code": "T-1",
         "profile_type_id": pt["profile_type_id"],
         "target_length_mm": 1500,
         "target_angle_deg": 90.0,
         "quantity": 5},
        "order_id",
    )
    yield d
    d.close()


def test_threshold_loading(db):
    ctl = DefectController(db)
    assert "length_mm" in ctl._thresholds
    assert ctl._thresholds["length_mm"]["max"] == 1.0
    assert ctl._thresholds["length_mm"]["min"] == -1.0
    assert ctl._thresholds["temp_c"]["max"] == 65.0
    # length_mm — это настройка станка
    assert ctl._thresholds["length_mm"]["machine_adjustable"] is True
    assert ctl._thresholds["vibration_mm_s"]["machine_adjustable"] is False


def test_length_within_tolerance_ok(db):
    ctl = DefectController(db)
    ev = ctl.check_metric(
        order_id=1, unit_index=1, metric="length_mm",
        value=1500.5, target=1500.0, state="q4",
    )
    assert ev is None  # отклонение 0.5 < 1.0 → ОК


def test_length_breach_creates_event(db):
    ctl = DefectController(db)
    ev = ctl.check_metric(
        order_id=1, unit_index=1, metric="length_mm",
        value=1502.5, target=1500.0, state="q4",
    )
    assert ev is not None
    assert ev.defect_code == "EDGE_DEFORMATION"
    assert ev.measured_value == pytest.approx(2.5)
    assert ev.threshold_value == 1.0
    assert ev.detected_state == "q4"
    assert ev.target_value == pytest.approx(1500.0)


def test_double_breach_not_duplicated(db):
    ctl = DefectController(db)
    e1 = ctl.check_metric(1, 1, "length_mm", 1505.0, "q4", target=1500.0)
    e2 = ctl.check_metric(1, 1, "length_mm", 1510.0, "q4", target=1500.0)
    assert e1 is not None
    assert e2 is None  # уже сработало по этой метрике


def test_temp_breach(db):
    ctl = DefectController(db)
    ev = ctl.check_metric(1, 1, "temp_c", 80.0, state="q3")
    assert ev is not None
    assert ev.defect_code == "SCRATCH"
    assert ev.threshold_value == 65.0


def test_persist_writes_row(db):
    ctl = DefectController(db)
    ev = ctl.check_metric(1, 1, "length_mm", 1505.0, "q4", target=1500.0)
    defect_id = ctl.persist(ev)
    row = db.fetch_one(
        "SELECT measured_value, threshold_value, detected_state, order_id, unit_index "
        "FROM defect WHERE defect_id=?",
        (defect_id,),
    )
    assert row["measured_value"] == pytest.approx(5.0)
    assert row["threshold_value"] == 1.0
    assert row["detected_state"] == "q4"
    assert row["order_id"] == 1
    assert row["unit_index"] == 1


def test_register_manual(db):
    """Ручная фиксация дефекта оператором (через web-форму)."""
    ctl = DefectController(db)
    defect_id = ctl.register_manual(
        order_id=1, unit_index=2,
        defect_code="SCRATCH",
        detected_state="q4",
        comment="Замечена царапина на корпусе",
    )
    row = db.fetch_one(
        "SELECT auto_detected, comment, unit_index FROM defect WHERE defect_id=?",
        (defect_id,),
    )
    assert row["auto_detected"] == 0
    assert row["unit_index"] == 2
    assert "царапина" in row["comment"].lower()


@pytest.mark.asyncio
async def test_handle_reading_async(db):
    ctl = DefectController(db)
    saw = db.fetch_one("SELECT equipment_id FROM equipment WHERE code='SAW'")
    await ctl.handle_reading(
        order_id=1, unit_index=1, equipment_id=saw["equipment_id"],
        equipment_code="SAW", metric="vibration_mm_s",
        value=8.0, state="q3", target_length=1500.0, target_angle=90.0,
    )
    rows = db.fetch_all("SELECT * FROM defect")
    assert len(rows) == 1
    assert rows[0]["measured_value"] == pytest.approx(8.0)
