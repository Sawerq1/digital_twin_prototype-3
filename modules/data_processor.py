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
    # key в NODEIDS, metric в БД, код оборудования
    ("blade_wear", "blade_wear", "SAW"),
    ("temp_c",     "temp_c",     "SAW"),
    ("vibration",  "vibration_mm_s", "SAW"),
    ("length_mm",  "length_mm",  "SAW"),
    ("angle_deg",  "angle_deg",  "SAW"),
]

NODEIDS = {
    "blade_wear": 'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.BladeWear',
    "temp_c":     'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.TempC',
    "vibration":  'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.Vibration_mm_s',
    "length_mm":  'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.MeasuredLength',
    "angle_deg":  'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.MeasuredAngle',
    "state":      'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.CurrentState',
    "unit":       'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.CurrentUnit',
    "order_id":   'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.CurrentOrderID',
    "profile":    'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.ProfileType',
    "target_len": 'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.TargetLength',
    "target_ang": 'ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL_Production.TargetAngle',
}


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
        # 1) состояние процесса — читаем напрямую из GVL_Production
        node_state = self.client.get_node(NODEIDS["state"])
        state_raw = await node_state.read_value()
        state_int = int(state_raw or 0)
        state = f"q{state_int}"  # 'q1'..'q10', как ожидал старый код

        node_unit = self.client.get_node(NODEIDS["unit"])
        unit_index = int(await node_unit.read_value() or 0)

        node_order = self.client.get_node(NODEIDS["order_id"])
        order_val = await node_order.read_value()
        order_code = str(order_val or "")

        node_profile = self.client.get_node(NODEIDS["profile"])
        profile_val = await node_profile.read_value()
        profile = str(profile_val or "")

        node_len = self.client.get_node(NODEIDS["target_len"])
        length_mm = float(await node_len.read_value() or 0.0)

        node_ang = self.client.get_node(NODEIDS["target_ang"])
        angle = float(await node_ang.read_value() or 0.0)

        # сигнал (пока не используем, оставляем пустым)
        signal = ""

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
        for key, metric, eq_code in SENSOR_MAP:
            try:
                node = self.client.get_node(NODEIDS[key])
                value = await node.read_value()
            except Exception as exc:  # pragma: no cover
                logger.warning("Не удалось прочитать %s (%s): %s", key, metric, exc)
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
                    self._current_order_id,
                    self._current_unit_index,
                    eq_code, metric, float(value),
                )

    def stop(self) -> None:
        self._stop.set()
