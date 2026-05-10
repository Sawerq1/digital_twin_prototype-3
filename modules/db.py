"""Слой доступа к данным.

Поддерживаются два бэкенда:
  * PostgreSQL — основной для боевого использования (схема `dt`);
  * SQLite — лёгкий режим для тестов и быстрого прототипирования.

Интерфейс одинаков для обоих бэкендов: класс ``Database`` инкапсулирует
открытие соединения и предоставляет высокоуровневые методы вставки и
выборки. Это позволяет всем остальным модулям (обработка данных, контроль
дефектов, отчёты) работать единообразно.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import psycopg2
    import psycopg2.extras
    HAS_PG = True
except ImportError:  # pragma: no cover
    HAS_PG = False

from configs.config import DB


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
SEED_PATH = Path(__file__).resolve().parent.parent / "db" / "seed.sql"


# SQLite-вариант схемы (без SCHEMA dt и без TimescaleDB-нюансов).
# Структура полностью соответствует db/schema.sql.
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_type (
    profile_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT NOT NULL UNIQUE,
    name_ru           TEXT NOT NULL,
    cut_correction_mm INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS defect_type (
    defect_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL UNIQUE,
    name_ru          TEXT NOT NULL,
    severity         INTEGER NOT NULL,
    is_critical      INTEGER NOT NULL DEFAULT 0,
    description      TEXT
);
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL UNIQUE,
    name_ru          TEXT NOT NULL,
    eq_type          TEXT NOT NULL,
    is_adjustable    INTEGER NOT NULL DEFAULT 1,
    opcua_node_id    TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS operator (
    operator_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name        TEXT NOT NULL,
    role             TEXT NOT NULL,
    login            TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS process_state (
    state_code       TEXT PRIMARY KEY,
    name_ru          TEXT NOT NULL,
    description      TEXT
);
CREATE TABLE IF NOT EXISTS production_order (
    order_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code        TEXT NOT NULL UNIQUE,
    profile_type_id   INTEGER NOT NULL,
    target_length_mm  INTEGER NOT NULL,
    target_angle_deg  REAL NOT NULL DEFAULT 90.0,
    quantity          INTEGER NOT NULL,
    needs_milling     INTEGER NOT NULL DEFAULT 0,
    color             TEXT,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'NEW'
);
CREATE TABLE IF NOT EXISTS sensor_reading (
    reading_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    equipment_id     INTEGER,
    order_id         INTEGER,
    unit_index       INTEGER,
    metric           TEXT NOT NULL,
    value_num        REAL,
    value_text       TEXT,
    quality          INTEGER NOT NULL DEFAULT 192
);
CREATE TABLE IF NOT EXISTS state_transition (
    transition_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         INTEGER NOT NULL,
    unit_index       INTEGER NOT NULL,
    from_state       TEXT,
    to_state         TEXT NOT NULL,
    signal_code      TEXT NOT NULL,
    occurred_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    operator_id      INTEGER
);
CREATE TABLE IF NOT EXISTS defect (
    defect_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         INTEGER NOT NULL,
    unit_index       INTEGER NOT NULL,
    defect_type_id   INTEGER NOT NULL,
    detected_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    detected_state   TEXT NOT NULL,
    equipment_id     INTEGER,
    operator_id      INTEGER,
    measured_value   REAL,
    target_value     REAL,
    threshold_value  REAL,
    auto_detected    INTEGER NOT NULL DEFAULT 1,
    comment          TEXT,
    resolved         INTEGER NOT NULL DEFAULT 0,
    resolved_at      TEXT
);
CREATE TABLE IF NOT EXISTS control_threshold (
    threshold_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_type_id       INTEGER,
    equipment_id          INTEGER,
    metric                TEXT NOT NULL,
    min_value             REAL,
    max_value             REAL,
    defect_type_id        INTEGER NOT NULL,
    is_machine_adjustable INTEGER NOT NULL DEFAULT 0,
    adjustment_hint       TEXT,
    UNIQUE (profile_type_id, equipment_id, metric)
);
CREATE TABLE IF NOT EXISTS defect_report (
    report_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    period_from      TEXT NOT NULL,
    period_to        TEXT NOT NULL,
    file_path        TEXT NOT NULL,
    format           TEXT NOT NULL,
    total_defects    INTEGER NOT NULL DEFAULT 0,
    generated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    generated_by     INTEGER
);
"""

SQLITE_SEED = """
INSERT OR IGNORE INTO profile_type (code, name_ru, cut_correction_mm) VALUES
    ('ALFA',   'Профиль ALFA',   -8),
    ('BEAM',   'Профиль BEAM',   -8),
    ('KORNER', 'Профиль KORNER', -8),
    ('KUBIK',  'Профиль KUBIK',  -4),
    ('LATEEN', 'Профиль LATEEN', -8);

INSERT OR IGNORE INTO defect_type (code, name_ru, severity, is_critical, description) VALUES
    ('DEFORMATION',      'Деформация',                3, 0, 'Выступы и углубления кольцеобразной формы'),
    ('COLOR_UNEVEN',     'Неравномерное окрашивание', 2, 0, 'Неравномерная толщина покрытия'),
    ('SCRATCH',          'Царапины',                  2, 0, 'Углубления неправильной формы от механических воздействий'),
    ('OBZOL',            'Обзол',                     4, 1, 'Непрокрас и деформация в местах крепления фиксаторов'),
    ('LENS_BREAK',       'Излом рассеивателя',        5, 1, 'Полное или частичное разрушение рассеивателя'),
    ('EDGE_DEFORMATION', 'Деформация краёв профиля',  4, 1, 'Износ диска пилы или фрезы');

INSERT OR IGNORE INTO equipment (code, name_ru, eq_type, is_adjustable, opcua_node_id) VALUES
    ('SAW',           'Дисковая пила',          'saw',           1, 'ns=2;s=Saw'),
    ('MILL',          'Фрезерный станок',       'mill',          1, 'ns=2;s=Mill'),
    ('MATERIAL_PREP', 'Этап подготовки материала','material_prep', 0, NULL);

INSERT OR IGNORE INTO process_state (state_code, name_ru, description) VALUES
    ('q1','Ожидание ТЗ',''),('q2','Подготовка материалов',''),('q3','Запил',''),
    ('q4','Проверка после запила',''),('q5','Выбор маршрута',''),
    ('q6','Дополнительная проверка',''),('q7','Подготовка комплектующих',''),
    ('q8','Сборка и маркировка',''),('q9','Упаковка',''),('q10','Перемещение на склад','');

INSERT OR IGNORE INTO operator (full_name, role, login) VALUES
    ('Иванов И. И.',  'production', 'ivanov'),
    ('Петров П. П.',  'production', 'petrov'),
    ('Сидорова С.С.', 'qc',         'sidorova'),
    ('Кузнецов А.А.', 'supervisor', 'kuznetsov');

INSERT OR IGNORE INTO control_threshold (
    profile_type_id, equipment_id, metric, min_value, max_value,
    defect_type_id, is_machine_adjustable, adjustment_hint
) VALUES
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'length_mm', -1.0, 1.0,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     1, 'Скорректировать упор пилы на разницу между фактической и целевой длиной'),
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'angle_deg', -0.5, 0.5,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     1, 'Скорректировать угол наклона диска пилы по электронному уровню'),
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'vibration_mm_s', NULL, 4.5,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     0, 'Остановить станок, проверить износ подшипников и затяжку диска'),
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'blade_wear', NULL, 0.85,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     0, 'Заменить пильный диск'),
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='MILL'),
     'temp_c', NULL, 65.0,
     (SELECT defect_type_id FROM defect_type WHERE code='SCRATCH'),
     0, 'Остановить станок до охлаждения, проверить систему охлаждения');
"""


class Database:
    """Унифицированный слой доступа к БД (PostgreSQL или SQLite)."""

    def __init__(self, backend: Optional[str] = None) -> None:
        self.backend = backend or DB.backend
        if self.backend == "postgres":
            if not HAS_PG:
                raise RuntimeError("psycopg2 не установлен")
            self._conn = psycopg2.connect(DB.pg_dsn)
            self._conn.autocommit = True
            with self._conn.cursor() as cur:
                cur.execute("SET search_path TO dt, public;")
        elif self.backend == "sqlite":
            Path(DB.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(DB.sqlite_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        else:
            raise ValueError(f"Неизвестный backend: {self.backend}")

    # ---------------------------------------------------------------- helpers

    @contextmanager
    def cursor(self):
        if self.backend == "postgres":
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
        else:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def _q(self, sql: str) -> str:
        """Преобразование плейсхолдеров %s -> ? для SQLite."""
        return sql.replace("%s", "?") if self.backend == "sqlite" else sql

    # --------------------------------------------------------- инициализация

    def init_schema(self) -> None:
        """Создание схемы и наполнение справочников (идемпотентно)."""
        if self.backend == "sqlite":
            self._conn.executescript(SQLITE_SCHEMA)
            self._conn.executescript(SQLITE_SEED)
            self._conn.commit()
        else:
            schema = SCHEMA_PATH.read_text(encoding="utf-8")
            seed = SEED_PATH.read_text(encoding="utf-8")
            with self._conn.cursor() as cur:
                cur.execute(schema)
                cur.execute(seed)

    # ----------------------------------------------------------- public API

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(self._q(sql), tuple(params))
            rows = cur.fetchall()
            if self.backend == "sqlite":
                return [dict(r) for r in rows]
            return rows  # уже dict через RealDictCursor

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self.cursor() as cur:
            cur.execute(self._q(sql), tuple(params))
            row = cur.fetchone()
            if row is None:
                return None
            return dict(row) if self.backend == "sqlite" else row

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.cursor() as cur:
            cur.execute(self._q(sql), tuple(params))
            return cur.rowcount

    def insert_returning_id(
        self, table: str, data: dict, id_col: str
    ) -> int:
        cols = list(data.keys())
        vals = list(data.values())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)
        if self.backend == "postgres":
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) RETURNING {id_col}"
            with self.cursor() as cur:
                cur.execute(sql, tuple(vals))
                return cur.fetchone()[id_col]
        else:
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
            with self.cursor() as cur:
                cur.execute(self._q(sql), tuple(vals))
                return cur.lastrowid

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def reset_sqlite() -> None:
    """Удалить файл sqlite — полезно для тестов."""
    p = Path(DB.sqlite_path)
    if p.exists() and DB.backend == "sqlite":
        os.unlink(p)
