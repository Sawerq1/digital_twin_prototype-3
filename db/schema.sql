-- =============================================================================
-- Схема базы данных ИАС мониторинга дефектов производства световых конструкций
-- СУБД: PostgreSQL 15+
-- Кодировка: UTF-8
-- =============================================================================
-- Структура соответствует архитектуре, заданной в главе 2:
--   * слой бизнес-процессов: заказ, этапы технологического процесса (КА q1..q10);
--   * слой данных: дефекты, измерения с датчиков (через OPC UA), отчёты;
--   * слой ссылочных справочников: профили, типы дефектов, оборудование,
--     операторы, состояния конечного автомата.
--
-- Особенности модели:
--   * profile_type содержит только справочные данные о профиле и поправку
--     к длине запила (см. п. 1.1.1.2 главы 1 — для разных сечений она разная);
--   * сущность workpiece исключена — её роль выполняет связка
--     defect.order_id + defect.unit_index, что упрощает модель и убирает
--     избыточные «партионные» записи (по требованию заказчика);
--   * этап подготовки материала (стадия q1) не привязан к станку —
--     equipment_id в defect и sensor_reading допускает NULL;
--   * control_threshold помечает параметры, регулируемые на станке
--     (например, длина запила и угол), отдельным флагом is_machine_adjustable.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS dt;
SET search_path TO dt, public;

-- -----------------------------------------------------------------------------
-- 1. СПРАВОЧНИКИ
-- -----------------------------------------------------------------------------

-- 1.1 Виды профилей светильников (см. п. 1.1.1.2 главы 1).
-- В отличие от других справочников хранит только код, наименование и
-- поправку к длине запила, т.к. остальные характеристики (сечение, размеры)
-- меняются от заказа к заказу и фиксируются в production_order.
CREATE TABLE IF NOT EXISTS profile_type (
    profile_type_id    SERIAL PRIMARY KEY,
    code               VARCHAR(16)  NOT NULL UNIQUE,    -- ALFA, BEAM, KORNER, KUBIK, LATEEN
    name_ru            VARCHAR(64)  NOT NULL,
    cut_correction_mm  INTEGER      NOT NULL DEFAULT 0  -- поправка к длине запила
                                                        -- ALFA -8, BEAM -8, KORNER -8, KUBIK -4, LATEEN -8
);

-- 1.2 Типы дефектов (см. п. 1.1.1.5).
-- Все 6 видов дефектов из главы 1 присутствуют:
--   1) Деформация            -> DEFORMATION
--   2) Неравномерное окраш.  -> COLOR_UNEVEN
--   3) Царапины              -> SCRATCH
--   4) Обзол                 -> OBZOL
--   5) Излом рассеивателя    -> LENS_BREAK
--   6) Деформация краёв      -> EDGE_DEFORMATION
CREATE TABLE IF NOT EXISTS defect_type (
    defect_type_id   SERIAL PRIMARY KEY,
    code             VARCHAR(32) NOT NULL UNIQUE,
    name_ru          VARCHAR(128) NOT NULL,
    severity         SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),
    is_critical      BOOLEAN NOT NULL DEFAULT FALSE,   -- блокирует дальнейшее производство
    description      TEXT
);

-- 1.3 Оборудование (см. п. 1.1.1.3).
-- Согласно постановке задачи, по каждому виду станка считается один экземпляр,
-- поэтому коды без номеров: SAW, MILL.
-- Дополнительно введено псевдо-«оборудование» MATERIAL_PREP — этап подготовки
-- материала (стадия q1), на которой первичная проверка материала на дефекты
-- ведётся без участия станка. Дефекты этого этапа связаны с самим материалом,
-- а не с настройками станка.
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id     SERIAL PRIMARY KEY,
    code             VARCHAR(32) NOT NULL UNIQUE,         -- SAW, MILL, MATERIAL_PREP
    name_ru          VARCHAR(128) NOT NULL,
    eq_type          VARCHAR(32) NOT NULL,                -- saw / mill / material_prep
    is_adjustable    BOOLEAN NOT NULL DEFAULT TRUE,       -- FALSE для material_prep
    opcua_node_id    VARCHAR(128),                        -- путь OPC UA, NULL для material_prep
    is_active        BOOLEAN NOT NULL DEFAULT TRUE
);

-- 1.4 Операторы производства
CREATE TABLE IF NOT EXISTS operator (
    operator_id      SERIAL PRIMARY KEY,
    full_name        VARCHAR(128) NOT NULL,
    role             VARCHAR(32) NOT NULL,                -- production / qc / supervisor
    login            VARCHAR(64) UNIQUE
);

-- 1.5 Состояния конечного автомата (см. таблицу 5 главы 2)
CREATE TABLE IF NOT EXISTS process_state (
    state_code       VARCHAR(8) PRIMARY KEY,              -- q1..q10
    name_ru          VARCHAR(128) NOT NULL,
    description      TEXT
);

-- -----------------------------------------------------------------------------
-- 2. ОПЕРАТИВНЫЕ ДАННЫЕ
-- -----------------------------------------------------------------------------

-- 2.1 Заказ / техническое задание.
-- Используется в первую очередь для сравнения целевых параметров
-- (длина, угол) с фактически полученными значениями после операций пилы
-- и фрезерования. Поле quantity заменяет отдельную таблицу партий.
CREATE TABLE IF NOT EXISTS production_order (
    order_id         SERIAL PRIMARY KEY,
    order_code       VARCHAR(32) NOT NULL UNIQUE,         -- например: 346.00.1500NSH
    profile_type_id  INTEGER NOT NULL REFERENCES profile_type(profile_type_id),
    target_length_mm INTEGER NOT NULL CHECK (target_length_mm > 0),
    target_angle_deg NUMERIC(5,2) NOT NULL DEFAULT 90.0,
    quantity         INTEGER NOT NULL CHECK (quantity > 0), -- сколько изделий в партии
    needs_milling    BOOLEAN NOT NULL DEFAULT FALSE,
    color            VARCHAR(32),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           VARCHAR(16) NOT NULL DEFAULT 'NEW'
                     CHECK (status IN ('NEW','IN_PROGRESS','DONE','CANCELLED'))
);

CREATE INDEX IF NOT EXISTS idx_order_status ON production_order(status);

-- 2.2 «Сырые» измерения от ПЛК через OPC UA — таблица большой нагрузки.
-- equipment_id допускает NULL для измерений на этапе подготовки материала
-- (когда оператор фиксирует визуальные дефекты вручную).
-- Совместима с TimescaleDB: можно сделать hypertable по полю ts.
CREATE TABLE IF NOT EXISTS sensor_reading (
    reading_id       BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    equipment_id     INTEGER REFERENCES equipment(equipment_id),
    order_id         INTEGER REFERENCES production_order(order_id),
    unit_index       INTEGER,                              -- номер изделия в партии (1..quantity)
    metric           VARCHAR(48) NOT NULL,                 -- length_mm, angle_deg, vibration_mm_s, blade_wear, temp_c
    value_num        NUMERIC(12,3),
    value_text       VARCHAR(64),
    quality          SMALLINT NOT NULL DEFAULT 192         -- StatusCode OPC UA (192 = Good)
);

CREATE INDEX IF NOT EXISTS idx_reading_ts    ON sensor_reading(ts DESC);
CREATE INDEX IF NOT EXISTS idx_reading_eq    ON sensor_reading(equipment_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reading_order ON sensor_reading(order_id);

-- 2.3 Журнал переходов конечного автомата (по сигналам y1..y10).
-- Привязывается напрямую к заказу и порядковому номеру изделия.
CREATE TABLE IF NOT EXISTS state_transition (
    transition_id    BIGSERIAL PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES production_order(order_id) ON DELETE CASCADE,
    unit_index       INTEGER NOT NULL,
    from_state       VARCHAR(8) REFERENCES process_state(state_code),
    to_state         VARCHAR(8) NOT NULL REFERENCES process_state(state_code),
    signal_code      VARCHAR(8) NOT NULL,                  -- y1..y10
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    operator_id      INTEGER REFERENCES operator(operator_id)
);

CREATE INDEX IF NOT EXISTS idx_transition_order ON state_transition(order_id);

-- 2.4 ДЕФЕКТЫ — центральная сущность мониторинга.
-- Связана напрямую с заказом и конкретным изделием в партии.
-- equipment_id может быть NULL для дефектов, обнаруженных на этапе
-- подготовки материала (q1) — они связаны с материалом, а не со станком.
CREATE TABLE IF NOT EXISTS defect (
    defect_id        BIGSERIAL PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES production_order(order_id) ON DELETE CASCADE,
    unit_index       INTEGER NOT NULL,                     -- номер изделия в партии (1..quantity)
    defect_type_id   INTEGER NOT NULL REFERENCES defect_type(defect_type_id),
    detected_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    detected_state   VARCHAR(8) NOT NULL REFERENCES process_state(state_code),
    equipment_id     INTEGER REFERENCES equipment(equipment_id),  -- NULL при дефектах материала
    operator_id      INTEGER REFERENCES operator(operator_id),
    measured_value   NUMERIC(12,3),
    target_value     NUMERIC(12,3),                        -- целевое значение из заказа (для сравнения)
    threshold_value  NUMERIC(12,3),                        -- порог, нарушение которого вызвало срабатывание
    auto_detected    BOOLEAN NOT NULL DEFAULT TRUE,
    comment          TEXT,
    resolved         BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_defect_order ON defect(order_id);
CREATE INDEX IF NOT EXISTS idx_defect_type  ON defect(defect_type_id);
CREATE INDEX IF NOT EXISTS idx_defect_dtm   ON defect(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_defect_state ON defect(detected_state);

-- 2.5 Контролируемые и регулируемые параметры (см. таблицу 2 главы 1).
-- Поле is_machine_adjustable показывает, можно ли исправить отклонение
-- настройкой станка (например, длина и угол запила корректируются
-- настройкой пилы — в этом случае требуется equipment_id).
-- adjustment_hint содержит подсказку оператору о том, как корректировать
-- параметр (например: «уменьшить длину на разницу между фактом и целью»).
CREATE TABLE IF NOT EXISTS control_threshold (
    threshold_id          SERIAL PRIMARY KEY,
    profile_type_id       INTEGER REFERENCES profile_type(profile_type_id), -- NULL = все профили
    equipment_id          INTEGER REFERENCES equipment(equipment_id),       -- к какому станку относится правило
    metric                VARCHAR(48) NOT NULL,
    min_value             NUMERIC(12,3),
    max_value             NUMERIC(12,3),
    defect_type_id        INTEGER NOT NULL REFERENCES defect_type(defect_type_id),
    is_machine_adjustable BOOLEAN NOT NULL DEFAULT FALSE,  -- можно ли поправить настройкой станка
    adjustment_hint       TEXT,                            -- инструкция оператору
    UNIQUE (profile_type_id, equipment_id, metric)
);

-- 2.6 Сформированные отчёты (метаданные)
CREATE TABLE IF NOT EXISTS defect_report (
    report_id        SERIAL PRIMARY KEY,
    period_from      TIMESTAMPTZ NOT NULL,
    period_to        TIMESTAMPTZ NOT NULL,
    file_path        TEXT NOT NULL,
    format           VARCHAR(8) NOT NULL CHECK (format IN ('PDF','XLSX','CSV')),
    total_defects    INTEGER NOT NULL DEFAULT 0,
    generated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    generated_by     INTEGER REFERENCES operator(operator_id)
);

-- -----------------------------------------------------------------------------
-- 3. ПРЕДСТАВЛЕНИЯ для аналитики
-- -----------------------------------------------------------------------------

-- 3.1 Статистика дефектов по типам
CREATE OR REPLACE VIEW v_defect_stats AS
SELECT  dt.code               AS defect_code,
        dt.name_ru            AS defect_name,
        COUNT(d.defect_id)    AS total,
        COUNT(d.defect_id) FILTER (WHERE d.resolved)        AS resolved,
        COUNT(d.defect_id) FILTER (WHERE NOT d.resolved)    AS open,
        ROUND(AVG(EXTRACT(EPOCH FROM (COALESCE(d.resolved_at, now()) - d.detected_at))/60.0)::numeric, 2)
                              AS avg_open_minutes
FROM defect_type dt
LEFT JOIN defect d ON d.defect_type_id = dt.defect_type_id
GROUP BY dt.code, dt.name_ru
ORDER BY total DESC;

-- 3.2 Статистика дефектов по профилям
CREATE OR REPLACE VIEW v_defect_by_profile AS
SELECT  pt.code                  AS profile_code,
        dt.code                  AS defect_code,
        COUNT(d.defect_id)       AS total
FROM defect d
JOIN production_order o ON o.order_id        = d.order_id
JOIN profile_type pt    ON pt.profile_type_id = o.profile_type_id
JOIN defect_type dt     ON dt.defect_type_id = d.defect_type_id
GROUP BY pt.code, dt.code
ORDER BY pt.code, total DESC;

-- 3.3 Уровень брака по заказам
CREATE OR REPLACE VIEW v_order_quality AS
SELECT  o.order_id,
        o.order_code,
        pt.code                                                    AS profile,
        o.quantity,
        COUNT(DISTINCT d.unit_index)                               AS defective_units,
        ROUND(100.0 * COUNT(DISTINCT d.unit_index) /
              NULLIF(o.quantity, 0), 2)                            AS defect_rate_pct
FROM production_order o
JOIN profile_type pt ON pt.profile_type_id = o.profile_type_id
LEFT JOIN defect d   ON d.order_id = o.order_id
GROUP BY o.order_id, o.order_code, pt.code, o.quantity;
