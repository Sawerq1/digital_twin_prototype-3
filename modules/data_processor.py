"""Модуль сбора и обработки данных.

Подключается к OPC UA серверу (имитатору ПЛК CodeSys или реальному ПЛК),
периодически опрашивает интересующие узлы, нормализует значения
и сохраняет их в таблицу ``sensor_reading``. Также фиксирует переходы
конечного автомата в таблицу ``state_transition``.

В соответствии с переработанной моделью БД сущность ``workpiece``
исключена: каждое измерение и каждый переход привязываются к паре
(order_id, unit_index) — заказ и порядковый номер изделия в партии.

Это «слой приложений → сетевой слой» из ArchiMate-модели главы 2.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from asyncua import Client

from configs.config import OPCUA
from modules.db import Database

logger = logging.getLogger("data-processor")


# Какие переменные опрашиваем — список (browse_path, metric, equipment_code).
# Имена объектов соответствуют справочнику equipment.
SENSOR_MAP = [
    (["0:Objects", "2:Saw",  "2:BladeWear"],   "blade_wear",     "SAW"),
    (["0:Objects", "2:Saw",  "2:Temperature"], "temp_c",         "SAW"),
    (["0:Objects", "2:Saw",  "2:Vibration"],   "vibration_mm_s", "SAW"),
    (["0:Objects", "2:Saw",  "2:CutLengthMm"], "length_mm",      "SAW"),
    (["0:Objects", "2:Saw",  "2:CutAngleDeg"], "angle_deg",      "SAW"),
    (["0:Objects", "2:Mill", "2:ToolWear"],    "blade_wear",     "MILL"),
    (["0:Objects", "2:Mill", "2:Temperature"], "temp_c",         "MILL"),
    (["0:Objects", "2:Mill", "2:Vibration"],   "vibration_mm_s", "MILL"),
]


class DataProcessor:
    """OPC UA клиент + ETL."""

    def __init__(self, db: Database, endpoint: str = OPCUA.endpoint) -> None:
        self.db = db
        self.endpoint = endpoint
        self.client: Optional[Client] = None
        self._equipment_cache: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._last_state: Optional[str] = None
        self._last_unit_key: Optional[tuple[int, int]] = None  # (order_id, unit_index)
        self._current_order_id: Optional[int] = None
        self._current_unit_index: Optional[int] = None

        # callbacks для модуля контроля
        self.on_reading = None  # type: ignore[assignment]
        self.on_state_change = None  # type: ignore[assignment]

    # ----------------------------------------------------------- equipment

    def _equipment_id(self, code: str) -> int:
        if code in self._equipment_cache:
            return self._equipment_cache[code]
        row = self.db.fetch_one(
            "SELECT equipment_id FROM equipment WHERE code = %s", (code,)
        )
        if row is None:
            raise RuntimeError(f"Оборудование {code} не зарегистрировано в БД")
        self._equipment_cache[code] = row["equipment_id"]
        return self._equipment_cache[code]

    # ------------------------------------------------------------- заказ

    def _ensure_order(self, order_code: str, profile_code: str,
                      length_mm: float, angle: float) -> int:
        """Возвращает order_id, создаёт заказ при необходимости."""
        order = self.db.fetch_one(
            "SELECT order_id FROM production_order WHERE order_code = %s",
            (order_code,),
        )
        if order:
            return order["order_id"]

        profile = self.db.fetch_one(
            "SELECT profile_type_id FROM profile_type WHERE code = %s",
            (profile_code,),
        )
        if profile is None:
            raise RuntimeError(f"Неизвестный профиль {profile_code}")

        return self.db.insert_returning_id(
            "production_order",
            {
                "order_code":       order_code,
                "profile_type_id":  profile["profile_type_id"],
                "target_length_mm": int(length_mm),
                "target_angle_deg": float(angle),
                "quantity":         1,    # минимально допустимая партия;
                                          # будет увеличена при появлении новых
                                          # unit_index, см. _bump_quantity_if_needed
                "needs_milling":    0,
                "status":           "IN_PROGRESS",
            },
            "order_id",
        )

    def _bump_quantity_if_needed(self, order_id: int, unit_index: int) -> None:
        """Если изделие с unit_index появилось впервые и > quantity, увеличить quantity."""
        row = self.db.fetch_one(
            "SELECT quantity FROM production_order WHERE order_id = %s",
            (order_id,),
        )
        if row and unit_index > row["quantity"]:
            self.db.execute(
                "UPDATE production_order SET quantity = %s WHERE order_id = %s",
                (unit_index, order_id),
            )

    # ------------------------------------------------------------ переходы

    def _record_transition(self, order_id: int, unit_index: int,
                           from_state: Optional[str], to_state: str,
                           signal: str) -> None:
        self.db.execute(
            "INSERT INTO state_transition "
            "(order_id, unit_index, from_state, to_state, signal_code) "
            "VALUES (%s, %s, %s, %s, %s)",
            (order_id, unit_index, from_state, to_state, signal),
        )
        if to_state == "q10":
            # последняя стадия партии — отметим заказ завершённым,
            # если все изделия пройдены (упрощённая эвристика для прототипа)
            self.db.execute(
                "UPDATE production_order SET status = 'DONE', "
                "finished_at = CURRENT_TIMESTAMP "
                "WHERE order_id = %s AND status != 'DONE'",
                (order_id,),
            )

    # -------------------------------------------------------- основной цикл

    async def run(self, max_iterations: Optional[int] = None) -> None:
        """Подключается к OPC UA и в цикле забирает данные."""
        self.client = Client(url=self.endpoint)
        await self.client.connect()
        try:
            iteration = 0
            while not self._stop.is_set():
                await self._poll_once()
                iteration += 1
                if max_iterations is not None and iteration >= max_iterations:
                    break
                await asyncio.sleep(OPCUA.poll_interval_s)
        finally:
            await self.client.disconnect()

    async def _poll_once(self) -> None:
        assert self.client is not None
        # 1) состояние процесса
        proc = await self.client.nodes.objects.get_child(["2:Process"])
        state = await (await proc.get_child(["2:CurrentState"])).read_value()
        signal = await (await proc.get_child(["2:CurrentSignal"])).read_value()
        unit_index = int(await (await proc.get_child(["2:CurrentUnit"])).read_value() or 0)
        order_code = await (await proc.get_child(["2:CurrentOrder"])).read_value()
        profile = await (await proc.get_child(["2:CurrentProfile"])).read_value()
        length_mm = await (await proc.get_child(["2:TargetLengthMm"])).read_value()
        angle = await (await proc.get_child(["2:TargetAngleDeg"])).read_value()

        if not order_code or unit_index <= 0:
            return

        # обеспечить наличие заказа
        unit_key = (order_code, unit_index)
        if unit_key != self._last_unit_key:
            order_id = self._ensure_order(order_code, profile, length_mm, angle)
            self._bump_quantity_if_needed(order_id, unit_index)
            self._current_order_id = order_id
            self._current_unit_index = unit_index
            self._last_unit_key = unit_key
            self._last_state = None

        assert self._current_order_id is not None
        assert self._current_unit_index is not None

        # фиксация перехода
        if (state != self._last_state
                and self._last_state is not None):
            self._record_transition(
                self._current_order_id, self._current_unit_index,
                self._last_state, state, signal or "",
            )
            if self.on_state_change:
                await self.on_state_change(
                    self._current_order_id,
                    self._current_unit_index,
                    self._last_state, state, signal,
                )
        self._last_state = state

        # 2) сенсорные показания
        for path, metric, eq_code in SENSOR_MAP:
            try:
                node = await self.client.nodes.root.get_child(path)
                value = await node.read_value()
            except Exception as exc:  # pragma: no cover
                logger.warning("Не удалось прочитать %s: %s", path, exc)
                continue

            eq_id = self._equipment_id(eq_code)
            self.db.execute(
                "INSERT INTO sensor_reading "
                "(equipment_id, order_id, unit_index, metric, value_num) "
                "VALUES (%s, %s, %s, %s, %s)",
                (eq_id, self._current_order_id, self._current_unit_index,
                 metric, float(value)),
            )

            if self.on_reading:
                await self.on_reading(
                    order_id=self._current_order_id,
                    unit_index=self._current_unit_index,
                    equipment_id=eq_id,
                    equipment_code=eq_code,
                    metric=metric,
                    value=float(value),
                    state=state,
                    target_length=float(length_mm),
                    target_angle=float(angle),
                )

    def stop(self) -> None:
        self._stop.set()
