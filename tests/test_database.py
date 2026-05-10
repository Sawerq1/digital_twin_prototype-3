"""Тесты слоя БД и справочников."""

import pytest

from configs import config as cfg
from modules.db import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.DB, "sqlite_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(cfg.DB, "backend", "sqlite")
    d = Database(backend="sqlite")
    d.init_schema()
    yield d
    d.close()


def test_seeded_profiles(db):
    rows = db.fetch_all("SELECT code, cut_correction_mm FROM profile_type ORDER BY code")
    codes = [r["code"] for r in rows]
    assert codes == ["ALFA", "BEAM", "KORNER", "KUBIK", "LATEEN"]
    # KUBIK имеет особую поправку -4, остальные -8
    by = {r["code"]: r["cut_correction_mm"] for r in rows}
    assert by["KUBIK"] == -4
    assert by["ALFA"] == -8
    assert by["LATEEN"] == -8


def test_seeded_defect_types(db):
    rows = db.fetch_all("SELECT code, is_critical FROM defect_type")
    codes = {r["code"] for r in rows}
    assert {"OBZOL", "LENS_BREAK", "EDGE_DEFORMATION", "DEFORMATION",
            "COLOR_UNEVEN", "SCRATCH"} <= codes
    crit = {r["code"] for r in rows if r["is_critical"]}
    assert {"OBZOL", "LENS_BREAK", "EDGE_DEFORMATION"} <= crit


def test_seeded_equipment(db):
    rows = db.fetch_all("SELECT code, is_adjustable FROM equipment ORDER BY code")
    by = {r["code"]: r["is_adjustable"] for r in rows}
    assert {"SAW", "MILL", "MATERIAL_PREP"} <= set(by.keys())
    # этап подготовки материала не настраивается
    assert by["MATERIAL_PREP"] == 0
    assert by["SAW"] == 1


def test_thresholds_loaded(db):
    rows = db.fetch_all("SELECT metric, is_machine_adjustable FROM control_threshold")
    metrics = {r["metric"] for r in rows}
    assert {"length_mm", "angle_deg", "vibration_mm_s", "blade_wear", "temp_c"} == metrics
    # длина и угол связаны с настройками станка
    by = {r["metric"]: r["is_machine_adjustable"] for r in rows}
    assert by["length_mm"] == 1
    assert by["angle_deg"] == 1
    # вибрация и износ требуют сервиса, не настройки
    assert by["vibration_mm_s"] == 0


def test_order_and_defect_lifecycle(db):
    """Заказ + дефект по (order_id, unit_index) — без таблицы workpiece."""
    pt = db.fetch_one("SELECT profile_type_id FROM profile_type WHERE code='ALFA'")
    order_id = db.insert_returning_id(
        "production_order",
        {"order_code": "TEST-1",
         "profile_type_id": pt["profile_type_id"],
         "target_length_mm": 1500,
         "target_angle_deg": 90.0,
         "quantity": 5},
        "order_id",
    )
    assert isinstance(order_id, int) and order_id > 0

    # Переход состояний — теперь по (order_id, unit_index)
    db.execute(
        "INSERT INTO state_transition (order_id, unit_index, from_state, to_state, signal_code) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, 1, "q1", "q2", "y1"),
    )
    rows = db.fetch_all(
        "SELECT to_state, signal_code FROM state_transition WHERE order_id=? AND unit_index=?",
        (order_id, 1),
    )
    assert rows[0]["to_state"] == "q2"
    assert rows[0]["signal_code"] == "y1"

    # Дефект
    edge = db.fetch_one("SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'")
    saw = db.fetch_one("SELECT equipment_id FROM equipment WHERE code='SAW'")
    db.insert_returning_id(
        "defect",
        {"order_id": order_id, "unit_index": 1,
         "defect_type_id": edge["defect_type_id"],
         "detected_state": "q4",
         "equipment_id": saw["equipment_id"],
         "measured_value": 2.5, "target_value": 1500.0,
         "threshold_value": 1.0, "auto_detected": 1},
        "defect_id",
    )
    rows = db.fetch_all(
        "SELECT measured_value FROM defect WHERE order_id=? AND unit_index=?",
        (order_id, 1),
    )
    assert rows[0]["measured_value"] == 2.5
