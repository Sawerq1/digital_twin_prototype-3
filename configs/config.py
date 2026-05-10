"""Конфигурация прототипа цифрового двойника.

Все параметры собраны в одном месте, чтобы при переносе в реальное окружение
(промышленный сервер, ПЛК CodeSys) изменения сводились к правке этого файла.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# Корень проекта вычисляется относительно этого файла,
# чтобы отчёты и БД сохранялись внутри папки прототипа,
# а не во временную /tmp.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = PROJECT_ROOT / "reports"
DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class OpcUaConfig:
    endpoint: str = os.getenv("DT_OPCUA_ENDPOINT", "opc.tcp://0.0.0.0:4840/dt/server/")
    namespace: str = os.getenv("DT_OPCUA_NS", "http://example.org/dt-lighting")
    poll_interval_s: float = 0.5  # период опроса клиентом


@dataclass
class DbConfig:
    # Для прототипа допускается SQLite-режим (быстрая проверка),
    # для боевого варианта — PostgreSQL (см. главу 3).
    backend: str = os.getenv("DT_DB_BACKEND", "sqlite")  # sqlite | postgres
    sqlite_path: str = os.getenv("DT_SQLITE_PATH", str(DATA_DIR / "dt_monitoring.db"))
    pg_dsn: str = os.getenv(
        "DT_PG_DSN",
        "host=localhost port=5432 dbname=dt_monitoring user=dt password=dt",
    )


@dataclass
class ReportConfig:
    out_dir: str = os.getenv("DT_REPORTS_DIR", str(REPORTS_DIR))
    company: str = "Производство световых конструкций"


OPCUA = OpcUaConfig()
DB = DbConfig()
REPORTS = ReportConfig()
