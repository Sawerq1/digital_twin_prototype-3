"""OPC UA сервер — имитация ПЛК CodeSys.

Зачем нужен. Целевая среда — связка CodeSys + Visual Components, где CodeSys
играет роль ПЛК и публикует данные процесса по OPC UA, а Visual Components
подписывается на них и анимирует 3D-модель цеха. На этапе разработки
прототипа ИАС CodeSys ещё не подключен; чтобы можно было параллельно
писать модули обработки и контроля дефектов, реализован программный
имитатор OPC UA-сервера. Структура адресного пространства совпадает с той,
которая будет создана в реальном CodeSys-проекте, — изменив только адрес
эндпоинта в ``configs/config.py``, можно перенаправить клиент с имитатора
на реальный ПЛК без изменения кода клиентских модулей.

Адресное пространство (под схему БД, в которой оборудование без номеров):
    Objects/
        Saw/        Status, BladeWear, Temperature, Vibration, CutLengthMm, CutAngleDeg
        Mill/       Status, ToolWear, Temperature, Vibration
        Process/    CurrentState (q1..q10), CurrentOrder, CurrentUnit (1..quantity),
                    CurrentProfile, CurrentSignal, TargetLengthMm, TargetAngleDeg
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from asyncua import Server, ua

from configs.config import OPCUA

logger = logging.getLogger("plc-sim")


# ----------------- состояния и сценарий конечного автомата ------------------

# Соответствие сигналов и переходов (из таблицы 5 главы 2)
TRANSITIONS = {
    "q1":  ("y1",  "q2"),
    "q2":  ("y2",  "q3"),
    "q3":  ("y3",  "q4"),
    "q4":  ("y4",  "q5"),
    "q5":  ("y52", "q6"),    # требуется фрезеровка
    "q6":  ("y6",  "q8"),
    "q8":  ("y8",  "q9"),
    "q9":  ("y9",  "q10"),
    "q10": ("y10", "q1"),
}


class PlcSimulator:
    """Программная модель ПЛК и оборудования."""

    def __init__(self, endpoint: str = OPCUA.endpoint, namespace: str = OPCUA.namespace) -> None:
        self.endpoint = endpoint
        self.namespace = namespace
        self.server = Server()
        self._stop = asyncio.Event()
        self._serial_counter = 1000

    async def init(self) -> None:
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("DT Lighting PLC Simulator")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.idx = await self.server.register_namespace(self.namespace)

        objects = self.server.nodes.objects

        # ---- Дисковая пила (Saw)
        self.saw = await objects.add_object(self.idx, "Saw")
        self.var_saw_status = await self.saw.add_variable(self.idx, "Status", "IDLE")
        self.var_saw_blade_wear = await self.saw.add_variable(self.idx, "BladeWear", 0.20)
        self.var_saw_temp = await self.saw.add_variable(self.idx, "Temperature", 24.0)
        self.var_saw_vib = await self.saw.add_variable(self.idx, "Vibration", 1.2)
        self.var_saw_len = await self.saw.add_variable(self.idx, "CutLengthMm", 0.0)
        self.var_saw_angle = await self.saw.add_variable(self.idx, "CutAngleDeg", 90.0)

        # ---- Фрезерный станок (Mill)
        self.mill = await objects.add_object(self.idx, "Mill")
        self.var_mill_status = await self.mill.add_variable(self.idx, "Status", "IDLE")
        self.var_mill_tool_wear = await self.mill.add_variable(self.idx, "ToolWear", 0.15)
        self.var_mill_temp = await self.mill.add_variable(self.idx, "Temperature", 25.0)
        self.var_mill_vib = await self.mill.add_variable(self.idx, "Vibration", 0.9)

        # ---- Процесс (конечный автомат)
        self.proc = await objects.add_object(self.idx, "Process")
        self.var_state = await self.proc.add_variable(self.idx, "CurrentState", "q1")
        # CurrentUnit — порядковый номер изделия в партии заказа (1..quantity)
        self.var_unit = await self.proc.add_variable(self.idx, "CurrentUnit", 0)
        self.var_profile = await self.proc.add_variable(self.idx, "CurrentProfile", "")
        self.var_signal = await self.proc.add_variable(self.idx, "CurrentSignal", "")
        self.var_order = await self.proc.add_variable(self.idx, "CurrentOrder", "")
        self.var_target_len = await self.proc.add_variable(self.idx, "TargetLengthMm", 0.0)
        self.var_target_angle = await self.proc.add_variable(self.idx, "TargetAngleDeg", 90.0)

        # Все переменные доступны только на чтение — сценарий пишет сам сервер
        for v in [
            self.var_saw_status, self.var_saw_blade_wear, self.var_saw_temp,
            self.var_saw_vib, self.var_saw_len, self.var_saw_angle,
            self.var_mill_status, self.var_mill_tool_wear, self.var_mill_temp,
            self.var_mill_vib,
            self.var_state, self.var_unit, self.var_profile, self.var_signal,
            self.var_order, self.var_target_len, self.var_target_angle,
        ]:
            await v.set_writable(False)

    # ---------------------- сценарий производства ---------------------------

    async def run_scenario(self) -> None:
        """Бесконечный цикл: эмуляция прохождения заготовок по КА."""
        orders = [
            # (order_code, profile, length_mm, angle_deg, needs_milling)
            ("346.00.1500NSH", "ALFA",   1500, 90.0, False),
            ("100.00.2000NSH", "BEAM",   2000, 45.0, True),
            ("220.00.1200NSH", "KUBIK",  1200, 90.0, True),
            ("550.00.0900NSH", "KORNER",  900, 90.0, False),
            ("770.00.1800NSH", "LATEEN", 1800, 90.0, False),
        ]
        order_idx = 0
        unit_in_order = 0
        while not self._stop.is_set():
            order_code, profile, length_mm, angle, needs_mill = orders[order_idx % len(orders)]
            order_idx += 1
            unit_in_order += 1

            await self.var_order.write_value(order_code)
            await self.var_profile.write_value(profile)
            await self.var_unit.write_value(int(unit_in_order))
            await self.var_target_len.write_value(float(length_mm))
            await self.var_target_angle.write_value(float(angle))
            logger.info("Старт изделия #%d заказ %s профиль %s", unit_in_order, order_code, profile)

            # Прогон по состояниям
            state = "q1"
            while state != "q1" or not await self._just_started(state):
                signal, next_state = TRANSITIONS.get(state, (None, None))
                if signal is None:
                    break

                # Обход маршрута: если фрезеровка не требуется, q5 -> q8 (сигнал y51)
                if state == "q5" and not needs_mill:
                    signal, next_state = "y51", "q8"

                await self.var_state.write_value(state)
                await self.var_signal.write_value(signal)
                await self._simulate_state(state, length_mm, angle)
                await asyncio.sleep(0.4)  # эмуляция длительности этапа

                state = next_state
                if state == "q1":
                    break

            # Сброс
            await self.var_state.write_value("q1")
            await self.var_signal.write_value("")
            await asyncio.sleep(0.3)

    async def _just_started(self, state: str) -> bool:
        return False

    async def _simulate_state(self, state: str, length_mm: int, angle: float) -> None:
        """Подкладывает значения переменных в зависимости от текущего состояния."""
        if state == "q3":  # запил
            await self.var_saw_status.write_value("CUTTING")
            # износ диска постепенно растёт; иногда возникает аномалия
            wear = float(await self.var_saw_blade_wear.read_value())
            wear = min(0.99, wear + random.uniform(0.001, 0.01))
            await self.var_saw_blade_wear.write_value(wear)
            await self.var_saw_temp.write_value(round(40 + random.uniform(0, 30), 2))
            await self.var_saw_vib.write_value(round(1.5 + random.uniform(0, 4.0), 2))
            # фактический результат запила: в норме ~ length_mm ± 0.3 мм,
            # при сильно изношенном диске уход больше
            tolerance = 0.3 + (wear - 0.6) * 5 if wear > 0.6 else 0.3
            actual_len = length_mm + random.uniform(-tolerance, tolerance)
            actual_angle = angle + random.uniform(-0.3, 0.3) * (1 + wear)
            await self.var_saw_len.write_value(round(actual_len, 2))
            await self.var_saw_angle.write_value(round(actual_angle, 2))

        elif state == "q4":
            await self.var_saw_status.write_value("IDLE")

        elif state == "q6":  # фрезеровка
            await self.var_mill_status.write_value("MILLING")
            tw = float(await self.var_mill_tool_wear.read_value())
            tw = min(0.99, tw + random.uniform(0.002, 0.012))
            await self.var_mill_tool_wear.write_value(tw)
            await self.var_mill_temp.write_value(round(35 + random.uniform(0, 35), 2))
            await self.var_mill_vib.write_value(round(1.0 + random.uniform(0, 5.0), 2))

        else:
            # промежуточные состояния — пилы и фрезы простаивают
            if state in ("q1", "q2", "q5", "q8", "q9", "q10"):
                await self.var_saw_status.write_value("IDLE")
                await self.var_mill_status.write_value("IDLE")

    # ---------------------- управление жизненным циклом ---------------------

    async def start(self) -> None:
        await self.init()
        await self.server.start()
        logger.info("OPC UA сервер запущен на %s", self.endpoint)

    async def stop(self) -> None:
        self._stop.set()
        await self.server.stop()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    sim = PlcSimulator()
    await sim.start()
    try:
        await sim.run_scenario()
    finally:
        await sim.stop()


if __name__ == "__main__":
    asyncio.run(main())
