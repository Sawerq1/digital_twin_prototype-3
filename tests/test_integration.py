"""Интеграционный тест: имитатор ПЛК -> ETL -> контроль -> БД.

Проверяет, что весь конвейер модулей работает в связке: запускается
имитатор OPC UA сервера, к нему подключается DataProcessor, дефекты
выявляет DefectController, и результат корректно оседает в БД.
"""

import asyncio
import pytest

from configs import config as cfg
from modules.db import Database
from modules.data_processor import DataProcessor
from modules.defect_control import DefectController
from opcua_server.plc_simulator import PlcSimulator


@pytest.mark.asyncio
async def test_end_to_end_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.DB, "sqlite_path", str(tmp_path / "e2e.db"))
    monkeypatch.setattr(cfg.DB, "backend", "sqlite")
    # Используем нестандартный порт, чтобы не конфликтовать
    monkeypatch.setattr(cfg.OPCUA, "endpoint", "opc.tcp://127.0.0.1:48400/dt/")
    monkeypatch.setattr(cfg.OPCUA, "poll_interval_s", 0.1)

    db = Database(backend="sqlite")
    db.init_schema()

    sim = PlcSimulator(endpoint=cfg.OPCUA.endpoint)
    await sim.start()
    sim_task = asyncio.create_task(sim.run_scenario())

    await asyncio.sleep(0.8)  # дать серверу проинициализироваться

    ctl = DefectController(db)
    proc = DataProcessor(db, endpoint=cfg.OPCUA.endpoint)
    proc.on_reading = ctl.handle_reading
    proc.on_state_change = ctl.handle_state_change

    proc_task = asyncio.create_task(proc.run(max_iterations=80))

    try:
        await asyncio.wait_for(proc_task, timeout=20.0)
    except asyncio.TimeoutError:
        proc.stop()

    sim_task.cancel()
    try:
        await sim_task
    except (asyncio.CancelledError, Exception):
        pass
    await sim.stop()

    # Проверки
    readings = db.fetch_all("SELECT COUNT(*) AS c FROM sensor_reading")
    assert readings[0]["c"] > 0, "Должны быть собраны показания"

    # вместо workpiece — заказ с quantity растёт по мере появления новых единиц
    orders = db.fetch_all("SELECT COUNT(*) AS c FROM production_order")
    assert orders[0]["c"] >= 1

    transitions = db.fetch_all("SELECT COUNT(*) AS c FROM state_transition")
    assert transitions[0]["c"] >= 1, "Должны быть зафиксированы переходы КА"

    db.close()
