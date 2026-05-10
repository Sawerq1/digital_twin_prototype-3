-- Начальное наполнение справочников ИАС мониторинга дефектов
SET search_path TO dt, public;

-- Профили (см. п. 1.1.1.2 главы 1).
-- Поправка к длине запила (cut_correction_mm) определяется конструкцией
-- профиля: для прямоугольных и угловых сечений требуется -8 мм на компенсацию
-- заглушек и торцевой обработки, для KUBIK достаточно -4 мм.
INSERT INTO profile_type (code, name_ru, cut_correction_mm) VALUES
    ('ALFA',   'Профиль ALFA',   -8),
    ('BEAM',   'Профиль BEAM',   -8),
    ('KORNER', 'Профиль KORNER', -8),
    ('KUBIK',  'Профиль KUBIK',  -4),
    ('LATEEN', 'Профиль LATEEN', -8)
ON CONFLICT (code) DO NOTHING;

-- Типы дефектов (см. п. 1.1.1.5 главы 1) — все 6 видов из главы 1.
INSERT INTO defect_type (code, name_ru, severity, is_critical, description) VALUES
    ('DEFORMATION',      'Деформация',                   3, FALSE,
        'Периодически повторяющиеся выступы и углубления кольцеобразной формы или продольные/поперечные углубления с неровными краями'),
    ('COLOR_UNEVEN',     'Неравномерное окрашивание',    2, FALSE,
        'Неравномерность толщины покрытия после анодирования / адсорбционного окрашивания'),
    ('SCRATCH',          'Царапины',                     2, FALSE,
        'Углубления неправильной формы и произвольного направления, образованные механически'),
    ('OBZOL',            'Обзол',                        4, TRUE,
        'Сочетание непрокраса и деформации в местах крепления фиксаторов при погружении в красочную ванну'),
    ('LENS_BREAK',       'Излом рассеивателя',           5, TRUE,
        'Полное или частичное разрушение рассеивателя заготовки'),
    ('EDGE_DEFORMATION', 'Деформация краёв профиля',     4, TRUE,
        'Частичное разрушение алюминиевого профиля из-за износа диска пилы или фрезы')
ON CONFLICT (code) DO NOTHING;

-- Оборудование (см. п. 1.1.1.3 главы 1).
-- Без номеров: считаем, что в производстве по одному станку каждого типа.
-- Этап MATERIAL_PREP — псевдо-«оборудование», обозначающее стадию подготовки
-- материала: первичная проверка ведётся вручную, привязки к станку нет
-- (is_adjustable = FALSE, opcua_node_id = NULL).
INSERT INTO equipment (code, name_ru, eq_type, is_adjustable, opcua_node_id) VALUES
    ('SAW',           'Дисковая пила',          'saw',           TRUE,  'ns=2;s=Saw'),
    ('MILL',          'Фрезерный станок',       'mill',          TRUE,  'ns=2;s=Mill'),
    ('MATERIAL_PREP', 'Этап подготовки материала','material_prep', FALSE, NULL)
ON CONFLICT (code) DO NOTHING;

-- Состояния конечного автомата (таблица 5 главы 2)
INSERT INTO process_state (state_code, name_ru, description) VALUES
    ('q1',  'Ожидание ТЗ',                          'Начальное состояние'),
    ('q2',  'Подготовка материалов и проверка',     'Первичная проверка дефектов хранения'),
    ('q3',  'Запил заготовки',                      'Запил по параметрам ТЗ'),
    ('q4',  'Проверка после запила',                'Проверка излома и деформации краёв'),
    ('q5',  'Выбор маршрута',                       'Ветвление по необходимости фрезерования'),
    ('q6',  'Дополнительная проверка',              'Проверка дефектов фрезерования'),
    ('q7',  'Подготовка комплектующих',             'Параллельный поток сборки'),
    ('q8',  'Сборка и маркировка',                  'Объединение заготовки и комплектующих'),
    ('q9',  'Упаковка',                             'Упаковка готового светильника'),
    ('q10', 'Перемещение на склад',                 'Завершение цикла')
ON CONFLICT (state_code) DO NOTHING;

-- Операторы
INSERT INTO operator (full_name, role, login) VALUES
    ('Иванов И. И.',  'production', 'ivanov'),
    ('Петров П. П.',  'production', 'petrov'),
    ('Сидорова С.С.', 'qc',         'sidorova'),
    ('Кузнецов А.А.', 'supervisor', 'kuznetsov')
ON CONFLICT (login) DO NOTHING;

-- Контролируемые и регулируемые параметры (таблица 2 главы 1).
--
-- length_mm и angle_deg — параметры станка, отклонение исправляется
-- настройкой пилы / фрезы, поэтому is_machine_adjustable = TRUE и
-- equipment_id ссылается на конкретный станок.
--
-- vibration_mm_s, blade_wear, temp_c — диагностические параметры, дефект
-- из-за их превышения не лечится настройкой станка (требуется обслуживание),
-- поэтому is_machine_adjustable = FALSE.
INSERT INTO control_threshold (
    profile_type_id, equipment_id, metric, min_value, max_value,
    defect_type_id, is_machine_adjustable, adjustment_hint
) VALUES
    -- Длина запила пилы — регулируется настройкой пилы
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'length_mm', -1.0, 1.0,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     TRUE,
     'Скорректировать упор пилы на разницу между фактической и целевой длиной'),
    -- Угол запила пилы — регулируется настройкой пилы
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'angle_deg', -0.5, 0.5,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     TRUE,
     'Скорректировать угол наклона диска пилы по электронному уровню'),
    -- Вибрация пилы — обслуживание оборудования
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'vibration_mm_s', NULL, 4.5,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     FALSE,
     'Остановить станок, проверить износ подшипников и затяжку диска'),
    -- Износ диска пилы — замена расходника
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='SAW'),
     'blade_wear', NULL, 0.85,
     (SELECT defect_type_id FROM defect_type WHERE code='EDGE_DEFORMATION'),
     FALSE,
     'Заменить пильный диск'),
    -- Температура шпинделя фрезы — обслуживание
    (NULL,
     (SELECT equipment_id FROM equipment WHERE code='MILL'),
     'temp_c', NULL, 65.0,
     (SELECT defect_type_id FROM defect_type WHERE code='SCRATCH'),
     FALSE,
     'Остановить станок до охлаждения, проверить систему охлаждения')
ON CONFLICT (profile_type_id, equipment_id, metric) DO NOTHING;
