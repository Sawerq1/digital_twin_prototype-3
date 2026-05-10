"""Модуль контроля и выявления дефектов.

Получает измерения и события КА от модуля обработки данных и сверяет их
с допустимыми диапазонами (таблица ``control_threshold``). При нарушении
порога фиксирует дефект в таблице ``defect`` и уведомляет подписчиков
(например, веб-дашборд или Visual Components).

Логика проверок основана на анализе дефектов из главы 1:
  * длина и угол запила вне допуска → деформация краёв (``EDGE_DEFORMATION``);
    регулируются настройками станка;
  * чрезмерная вибрация / температура / износ → требуют обслуживания
    оборудования и фиксируются как косвенные признаки.
Также модуль использует знание состояний КА: проверка длины валидна только
после перехода ``q3 → q4`` (см. главу 2).

В соответствии с переработанной моделью данных дефект привязывается
к (order_id, unit_index), а не к workpiece_id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from modules.db import Database

logger = logging.getLogger("defect-control")


@dataclass
class DefectEvent:
    order_id: int
    unit_index: int
    defect_code: str
    detected_state: str
    measured_value: float
    target_value: Optional[float]
    threshold_value: Optional[float]
    equipment_id: Optional[int]
    auto_detected: bool = True
    comment: str = ""


class DefectController:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._thresholds = self._load_thresholds()
        self._defect_codes = {
            row["code"]: row["defect_type_id"]
            for row in self.db.fetch_all(
                "SELECT defect_type_id, code FROM defect_type"
            )
        }
        # Хранит (order_id, unit_index, metric) -> уже зафиксирован,
        # чтобы не плодить дубли по одному изделию.
        self._raised: set[tuple[int, int, str]] = set()

        # callback для подписчиков (визуализация, веб-дашборд)
        self.on_defect = None  # type: ignore[assignment]

    # ----------------------------------------------------- загрузка порогов

    def _load_thresholds(self) -> dict[str, dict]:
        """Возвращает {metric: {min, max, defect_code, machine_adjustable, hint}}.

        Глобальные правила (profile_type_id IS NULL) применяются ко всем
        изделиям. Профильные пороги перекрывают глобальные.
        """
        rows = self.db.fetch_all(
            "SELECT ct.profile_type_id, ct.equipment_id, ct.metric, "
            "       ct.min_value, ct.max_value, "
            "       ct.is_machine_adjustable, ct.adjustment_hint, "
            "       dt.code AS defect_code "
            "FROM control_threshold ct JOIN defect_type dt "
            "  ON dt.defect_type_id = ct.defect_type_id"
        )
        out: dict[str, dict] = {}
        for r in rows:
            metric = r["metric"]
            if metric not in out or r["profile_type_id"] is not None:
                out[metric] = {
                    "min": r["min_value"],
                    "max": r["max_value"],
                    "defect_code": r["defect_code"],
                    "machine_adjustable": bool(r["is_machine_adjustable"]),
                    "hint": r["adjustment_hint"] or "",
                }
        return out

    # ----------------------------------------------- проверка одной метрики

    def check_metric(
        self,
        order_id: int,
        unit_index: int,
        metric: str,
        value: float,
        state: str,
        equipment_id: Optional[int] = None,
        target: Optional[float] = None,
    ) -> Optional[DefectEvent]:
        """Возвращает DefectEvent, если значение нарушает порог."""
        rule = self._thresholds.get(metric)
        if rule is None:
            return None

        # Для длины и угла «значение» — отклонение от целевого, а не абсолют
        compared = value
        if metric in ("length_mm", "angle_deg") and target is not None:
            compared = value - target

        # Не дублировать одно и то же по одному изделию/метрике
        key = (order_id, unit_index, metric)
        if key in self._raised:
            return None

        breached = False
        threshold_value: Optional[float] = None
        if rule["min"] is not None and compared < rule["min"]:
            breached, threshold_value = True, rule["min"]
        elif rule["max"] is not None and compared > rule["max"]:
            breached, threshold_value = True, rule["max"]
        if not breached:
            return None

        # Текст комментария зависит от того, регулируется ли параметр на станке
        if rule["machine_adjustable"]:
            comment = (f"Параметр {metric} вне допуска. "
                       f"Регулируется на станке. {rule['hint']}")
        else:
            comment = f"Параметр {metric} вне допуска. {rule['hint']}"

        ev = DefectEvent(
            order_id=order_id,
            unit_index=unit_index,
            defect_code=rule["defect_code"],
            detected_state=state,
            measured_value=float(compared),
            target_value=float(target) if target is not None else None,
            threshold_value=float(threshold_value) if threshold_value is not None else None,
            equipment_id=equipment_id,
            comment=comment,
        )
        self._raised.add(key)
        return ev

    # ------------------------------------------------------- запись дефекта

    def persist(self, event: DefectEvent) -> int:
        defect_type_id = self._defect_codes[event.defect_code]
        return self.db.insert_returning_id(
            "defect",
            {
                "order_id":        event.order_id,
                "unit_index":      event.unit_index,
                "defect_type_id":  defect_type_id,
                "detected_state":  event.detected_state,
                "equipment_id":    event.equipment_id,
                "measured_value":  event.measured_value,
                "target_value":    event.target_value,
                "threshold_value": event.threshold_value,
                "auto_detected":   1 if event.auto_detected else 0,
                "comment":         event.comment,
            },
            "defect_id",
        )

    # ----------------------------------- ручная фиксация дефекта (web/QC)

    def register_manual(
        self,
        order_id: int,
        unit_index: int,
        defect_code: str,
        detected_state: str,
        operator_id: Optional[int] = None,
        equipment_id: Optional[int] = None,
        comment: str = "",
    ) -> int:
        """Регистрация дефекта вручную (через веб-форму оператора).

        Используется для дефектов, которые невозможно поймать автоматически
        по показаниям датчиков: царапины, неравномерное окрашивание, обзол,
        излом рассеивателя, дефекты материала на стадии q1-q2.
        """
        defect_type_id = self._defect_codes[defect_code]
        return self.db.insert_returning_id(
            "defect",
            {
                "order_id":       order_id,
                "unit_index":     unit_index,
                "defect_type_id": defect_type_id,
                "detected_state": detected_state,
                "equipment_id":   equipment_id,
                "operator_id":    operator_id,
                "auto_detected":  0,
                "comment":        comment or "Зафиксировано оператором",
            },
            "defect_id",
        )

    # ------------------------------------------ async-callback из ETL

    async def handle_reading(
        self,
        order_id: int,
        unit_index: int,
        equipment_id: int,
        equipment_code: str,
        metric: str,
        value: float,
        state: str,
        target_length: float,
        target_angle: float,
    ) -> None:
        # Геометрические метрики имеют смысл только после запила (q4)
        if metric in ("length_mm", "angle_deg") and state not in ("q4", "q5", "q6", "q8"):
            return
        target = None
        if metric == "length_mm":
            target = target_length
        elif metric == "angle_deg":
            target = target_angle

        ev = self.check_metric(
            order_id=order_id,
            unit_index=unit_index,
            metric=metric,
            value=value,
            state=state,
            equipment_id=equipment_id,
            target=target,
        )
        if ev:
            defect_id = self.persist(ev)
            logger.warning(
                "Зафиксирован дефект id=%s order=%s unit=%s code=%s value=%.3f thr=%s",
                defect_id, ev.order_id, ev.unit_index, ev.defect_code,
                ev.measured_value, ev.threshold_value,
            )
            if self.on_defect:
                await self.on_defect(defect_id, ev)

    async def handle_state_change(
        self, order_id: int, unit_index: int,
        from_state: str, to_state: str, signal: str,
    ) -> None:
        # При переходе на q1 (новый цикл) сбрасываем «уже сработавшие»
        if to_state == "q1":
            self._raised = {
                k for k in self._raised
                if not (k[0] == order_id and k[1] == unit_index)
            }
