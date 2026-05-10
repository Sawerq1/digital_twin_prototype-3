# Прототип цифрового двойника производства светильников

Программная часть главы 3 ВКР. Реализует связку **CodeSys + Visual Components**
через **OPC UA**: на стороне CodeSys выполняется ПЛК-программа управления
производственным циклом (запил, фрезеровка, сборка), а на стороне ИАС
работают модули обработки данных, контроля дефектов и формирования отчётов.

```
┌──────────────┐  OPC UA   ┌────────────────┐   psycopg2   ┌─────────────┐
│  CodeSys     │──────────▶│ DataProcessor  │─────────────▶│ PostgreSQL  │
│  (или        │           │  (asyncua-cli) │              │  схема dt   │
│  имитатор)   │           └───────┬────────┘              └─────┬───────┘
└──────┬───────┘                   │ on_reading                  │
       │ OPC UA                    ▼                              │
       ▼                  ┌────────────────┐                      │
┌──────────────┐          │ DefectControl  │──────────────────────┤
│ Visual       │          │ (правила/ML)   │                      │
│ Components   │          └────────────────┘                      │
└──────────────┘                                                  │
                                                                  ▼
                                                       ┌──────────────────┐
                                                       │ ReportGenerator  │
                                                       │ PDF / XLSX / CSV │
                                                       └──────────────────┘
```

## Структура проекта

```
digital_twin_prototype/
├── configs/                   единая точка конфигурации
├── db/
│   ├── schema.sql             полная схема PostgreSQL
│   └── seed.sql               наполнение справочников
├── opcua_server/
│   └── plc_simulator.py       имитатор ПЛК CodeSys на asyncua
├── modules/
│   ├── db.py                  слой доступа к БД (PG/SQLite)
│   ├── data_processor.py      OPC UA клиент + ETL
│   ├── defect_control.py      контроль и выявление дефектов
│   └── reports.py             отчёты (CSV / XLSX / PDF)
├── tests/                     pytest-тесты модулей и end-to-end
├── reports/                   сюда складываются сформированные отчёты
└── main.py                    точка входа (server / demo / report)
```

## Установка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск демо за один шаг

```bash
cd digital_twin_prototype
python main.py demo --seconds 30
```

Что произойдёт:

1. На `opc.tcp://0.0.0.0:4840/dt/server/` поднимется OPC UA сервер-имитатор.
2. К нему подключится DataProcessor и начнёт писать данные в SQLite (`/tmp/dt_monitoring.db`).
3. DefectController в реальном времени будет проверять отклонения длины,
   угла, вибрации, температуры и износа диска.
4. По истечении 30 секунд будут сгенерированы отчёты PDF/XLSX/CSV
   в каталоге `/tmp/dt_reports/`.

## Перевод на боевой контур

В `configs/config.py`:

```python
DT_DB_BACKEND=postgres
DT_PG_DSN="host=db port=5432 dbname=dt user=dt password=..."
DT_OPCUA_ENDPOINT="opc.tcp://192.168.1.10:4840/codesys/"  # реальный ПЛК
```

Затем выполнить SQL-скрипты `db/schema.sql` и `db/seed.sql` в PostgreSQL,
после чего запускать только клиентскую часть:

```bash
python -c "from modules.db import Database; Database().init_schema()"
python -m modules.data_processor   # либо через main.py
```

Структура адресного пространства имитатора `Saw01/Mill01/Process` совпадает
с символическими именами тегов в CodeSys-проекте (см. главу 3, раздел 3.3).

## Тестирование

```bash
pytest -q
```

Тесты покрывают:

* инициализацию схемы и наполнение справочников;
* загрузку порогов и проверку выявления дефектов по разным метрикам;
* идемпотентность регистрации дефекта (без дублирования);
* генерацию CSV/XLSX/PDF-отчётов и фиксацию их в БД;
* интеграционный e2e: имитатор → ETL → контроль → запись в БД.

## Подключение Visual Components

В Visual Components используется встроенный OPC UA Client connector.
В нём указывается тот же эндпоинт — `opc.tcp://<host>:4840/dt/server/`,
импортируются переменные `Saw01.Status`, `Mill01.Status`,
`Process.CurrentState` и привязываются к Behaviors 3D-объектов
(вращение диска пилы, движение портала фрезеровки, индикатор состояния).
Никаких изменений в Python-коде не требуется — VC и ИАС подключаются к
одному и тому же серверу как два независимых клиента.
