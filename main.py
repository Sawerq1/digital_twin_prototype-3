"""Точка входа прототипа цифрового двойника.

Запускает связку:
    OPC UA сервер (имитатор CodeSys / реальный ПЛК)
        -> модуль обработки данных (asyncua-клиент + ETL)
            -> модуль контроля дефектов
                -> модуль формирования отчётов

Использование:

    # 1) Полный демонстрационный цикл (60 секунд работы + отчёт)
    python -m main demo

    # 2) Только запуск сервера-имитатора (для подключения Visual Components
    #    или внешнего CodeSys-OPC UA клиента)
    python -m main server

    # 3) Только сгенерировать отчёт за последние N дней
    python -m main report --days 7

    # 4) Запустить web-интерфейс (оператор / начальник участка)
    python -m main web --port 5000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta

from configs.config import OPCUA
from modules.data_processor import DataProcessor
from modules.db import Database
from modules.defect_control import DefectController
from modules.reports import ReportGenerator, ReportPeriod
from opcua_server.plc_simulator import PlcSimulator


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-15s | %(levelname)-7s | %(message)s",
    )


# ------------------------------------------------------------------ режимы

async def cmd_server() -> None:
    sim = PlcSimulator()
    await sim.start()
    print(f"OPC UA сервер слушает на {OPCUA.endpoint}")
    print("Подключите Visual Components / CodeSys / asyncua-клиент. Ctrl+C — выход.")
    try:
        await sim.run_scenario()
    except KeyboardInterrupt:
        pass
    finally:
        await sim.stop()


async def cmd_demo(duration_s: int = 30) -> None:
    db = Database()
    db.init_schema()

    sim = PlcSimulator()
    await sim.start()
    sim_task = asyncio.create_task(sim.run_scenario())

    # Дать серверу проинициализироваться
    await asyncio.sleep(1.0)

    controller = DefectController(db)
    processor = DataProcessor(db)
    processor.on_reading = controller.handle_reading
    processor.on_state_change = controller.handle_state_change

    proc_task = asyncio.create_task(processor.run())

    print(f"Демо-сессия {duration_s} с... Идёт сбор данных и контроль дефектов.")
    await asyncio.sleep(duration_s)

    # Останавливаем
    processor.stop()
    sim_task.cancel()
    proc_task.cancel()
    for t in (sim_task, proc_task):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    await sim.stop()

    # Отчёт
    period = ReportPeriod.last_n_days(1)
    rep = ReportGenerator(db)
    pdf_path = rep.generate_pdf(period)
    xlsx_path = rep.generate_xlsx(period)
    csv_path = rep.generate_csv(period)
    print(f"Сформированы отчёты:\n  {pdf_path}\n  {xlsx_path}\n  {csv_path}")

    # Краткая сводка
    stats = db.fetch_all(
        "SELECT dt.code, dt.name_ru, COUNT(d.defect_id) AS cnt "
        "FROM defect_type dt LEFT JOIN defect d "
        "ON d.defect_type_id = dt.defect_type_id "
        "GROUP BY dt.code, dt.name_ru ORDER BY cnt DESC"
    )
    print("\nСводка по дефектам:")
    for s in stats:
        print(f"  {s['code']:<20} {s['name_ru']:<30} {s['cnt']}")
    db.close()


async def cmd_report(days: int) -> None:
    db = Database()
    db.init_schema()
    period = ReportPeriod.last_n_days(days)
    rep = ReportGenerator(db)
    print("PDF :", rep.generate_pdf(period))
    print("XLSX:", rep.generate_xlsx(period))
    print("CSV :", rep.generate_csv(period))
    db.close()


# ----------------------------------------------------------------- main

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Прототип цифрового двойника")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("server", help="запустить только OPC UA имитатор")
    p_demo = sub.add_parser("demo", help="полный демонстрационный прогон")
    p_demo.add_argument("--seconds", type=int, default=30)
    p_rep = sub.add_parser("report", help="сформировать отчёт за период")
    p_rep.add_argument("--days", type=int, default=7)
    p_web = sub.add_parser("web", help="запустить web-интерфейс")
    p_web.add_argument("--port", type=int, default=5000)
    p_web.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    if args.cmd == "server":
        asyncio.run(cmd_server())
    elif args.cmd == "demo":
        asyncio.run(cmd_demo(args.seconds))
    elif args.cmd == "report":
        asyncio.run(cmd_report(args.days))
    elif args.cmd == "web":
        from web.app import create_app
        app = create_app()
        app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
