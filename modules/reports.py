"""Модуль формирования отчётов о дефектах.

Поддерживает три формата вывода:
  * CSV — машинно-читаемый, для последующей загрузки в BI;
  * XLSX — отчёт оператора и начальника участка с несколькими листами;
  * PDF — итоговый «бумажный» отчёт для приёмки и аудита.

Все отчёты опираются на одни и те же агрегаты, рассчитываемые в SQL
(представления ``v_defect_stats``, ``v_defect_by_profile``,
``v_order_quality``). Каждое сохранение регистрируется в таблице
``defect_report``.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from configs.config import REPORTS
from modules.db import Database

logger = logging.getLogger("reports")


# --- Шрифт с поддержкой кириллицы ---------------------------------------
def _find_cyrillic_ttf() -> Optional[str]:
    """Ищет TTF-шрифт с кириллицей в стандартных местах ОС и Python-пакетах.

    Порядок поиска:
    1) Явные пути для Linux / macOS / Windows;
    2) Шрифты, поставляемые вместе с matplotlib (DejaVuSans.ttf);
    3) Шрифты, поставляемые с reportlab;
    4) Шрифты OpenCV (DejaVuSans*).
    Возвращает абсолютный путь или None.
    """
    explicit = [
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        # macOS
        "/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        # Windows
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]
    for p in explicit:
        if os.path.exists(p):
            return p

    # matplotlib (всегда есть, если установлен пакет)
    try:
        import matplotlib
        mpl_dir = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
        for name in ("DejaVuSans.ttf", "DejaVuSerif.ttf"):
            p = mpl_dir / name
            if p.exists():
                return str(p)
    except Exception:
        pass

    # reportlab
    try:
        import reportlab
        rl_dir = Path(reportlab.__file__).parent / "fonts"
        for cand in ("Vera.ttf", "DejaVuSans.ttf"):
            p = rl_dir / cand
            if p.exists():
                return str(p)
    except Exception:
        pass

    # OpenCV (часто установлен)
    for p in (
        "/usr/local/lib/python3.13/site-packages/cv2/qt/fonts/DejaVuSans.ttf",
        "/usr/lib/python3/dist-packages/cv2/qt/fonts/DejaVuSans.ttf",
    ):
        if os.path.exists(p):
            return p
    return None


def _register_cyrillic_font() -> str:
    """Регистрирует и возвращает имя шрифта для PDF.
    Если кириллический TTF не найден — возвращает Helvetica с предупреждением.
    """
    path = _find_cyrillic_ttf()
    if path is None:
        logger.warning(
            "Кириллический TTF не найден. PDF будет использовать Helvetica — "
            "кириллица может не отобразиться. Установите шрифт DejaVu Sans или "
            "задайте переменную окружения DT_PDF_FONT с путём к TTF."
        )
        return "Helvetica"
    try:
        pdfmetrics.registerFont(TTFont("DTFont", path))
        # Регистрируем bold-вариант, если есть
        bold_path = path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
        if bold_path != path and os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont("DTFont-Bold", bold_path))
                pdfmetrics.registerFontFamily(
                    "DTFont", normal="DTFont", bold="DTFont-Bold"
                )
            except Exception:
                pass
        logger.info("Зарегистрирован шрифт PDF: %s", path)
        return "DTFont"
    except Exception as e:
        logger.warning("Не удалось зарегистрировать шрифт %s: %s", path, e)
        return "Helvetica"


# Позволяем явно указать шрифт через переменную окружения
_env_font = os.getenv("DT_PDF_FONT")
if _env_font and os.path.exists(_env_font):
    try:
        pdfmetrics.registerFont(TTFont("DTFont", _env_font))
    except Exception:
        pass


@dataclass
class ReportPeriod:
    period_from: datetime
    period_to: datetime

    @classmethod
    def last_n_days(cls, n: int = 7) -> "ReportPeriod":
        now = datetime.utcnow()
        return cls(period_from=now - timedelta(days=n), period_to=now)


class ReportGenerator:
    def __init__(self, db: Database, out_dir: Optional[str] = None) -> None:
        self.db = db
        self.out_dir = Path(out_dir or REPORTS.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.font = _register_cyrillic_font()

    # ------------------------------------------------------ агрегации SQL

    def _by_type(self, period: ReportPeriod) -> list[dict]:
        sql = """
        SELECT  dt.code  AS defect_code,
                dt.name_ru AS defect_name,
                COUNT(d.defect_id) AS total
        FROM defect_type dt
        LEFT JOIN defect d
            ON d.defect_type_id = dt.defect_type_id
           AND d.detected_at >= %s AND d.detected_at < %s
        GROUP BY dt.code, dt.name_ru
        ORDER BY total DESC, dt.code
        """
        return self.db.fetch_all(sql, (period.period_from, period.period_to))

    def _by_profile(self, period: ReportPeriod) -> list[dict]:
        sql = """
        SELECT  pt.code AS profile,
                COUNT(d.defect_id) AS total
        FROM profile_type pt
        LEFT JOIN production_order o ON o.profile_type_id = pt.profile_type_id
        LEFT JOIN defect d ON d.order_id = o.order_id
            AND d.detected_at >= %s AND d.detected_at < %s
        GROUP BY pt.code
        ORDER BY total DESC, pt.code
        """
        return self.db.fetch_all(sql, (period.period_from, period.period_to))

    def _by_state(self, period: ReportPeriod) -> list[dict]:
        sql = """
        SELECT  detected_state AS state,
                COUNT(*) AS total
        FROM defect
        WHERE detected_at >= %s AND detected_at < %s
        GROUP BY detected_state
        ORDER BY total DESC
        """
        return self.db.fetch_all(sql, (period.period_from, period.period_to))

    def _details(self, period: ReportPeriod, limit: int = 200) -> list[dict]:
        sql = """
        SELECT  d.detected_at,
                d.order_id,
                d.unit_index,
                pt.code AS profile,
                dty.code AS defect_code,
                dty.name_ru AS defect_name,
                d.detected_state,
                d.measured_value,
                d.target_value,
                d.threshold_value,
                e.code AS equipment
        FROM defect d
        JOIN production_order o ON o.order_id = d.order_id
        JOIN profile_type pt ON pt.profile_type_id = o.profile_type_id
        JOIN defect_type dty ON dty.defect_type_id = d.defect_type_id
        LEFT JOIN equipment e ON e.equipment_id = d.equipment_id
        WHERE d.detected_at >= %s AND d.detected_at < %s
        ORDER BY d.detected_at DESC
        LIMIT %s
        """
        return self.db.fetch_all(sql, (period.period_from, period.period_to, limit))

    # ----------------------------------------------------------- общий учёт

    def _register(self, period: ReportPeriod, file_path: Path, fmt: str, total: int) -> int:
        return self.db.insert_returning_id(
            "defect_report",
            {
                "period_from":   period.period_from,
                "period_to":     period.period_to,
                "file_path":     str(file_path),
                "format":        fmt,
                "total_defects": total,
            },
            "report_id",
        )

    # --------------------------------------------------------------- CSV

    def generate_csv(self, period: ReportPeriod) -> Path:
        rows = self._details(period)
        path = self.out_dir / f"defect_report_{period.period_to:%Y%m%d_%H%M%S}.csv"
        if rows:
            keys = list(rows[0].keys())
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for r in rows:
                    w.writerow(r)
        else:
            path.write_text("Нет данных за период\n", encoding="utf-8-sig")
        self._register(period, path, "CSV", len(rows))
        return path

    # -------------------------------------------------------------- XLSX

    def generate_xlsx(self, period: ReportPeriod) -> Path:
        path = self.out_dir / f"defect_report_{period.period_to:%Y%m%d_%H%M%S}.xlsx"
        wb = Workbook()
        # Лист 1: сводка
        ws1 = wb.active
        ws1.title = "Сводка"
        ws1.append(["Отчёт о дефектах производства светильников"])
        ws1.append([f"Период: {period.period_from:%Y-%m-%d %H:%M} — {period.period_to:%Y-%m-%d %H:%M}"])
        ws1.append([])
        ws1["A1"].font = Font(bold=True, size=14)

        # Дефекты по типам
        ws1.append(["По типу дефекта"])
        ws1.append(["Код", "Название", "Кол-во"])
        total = 0
        for r in self._by_type(period):
            ws1.append([r["defect_code"], r["defect_name"], r["total"]])
            total += int(r["total"])
        ws1.append(["", "Итого:", total])
        ws1.append([])

        # По профилям
        ws1.append(["По профилям"])
        ws1.append(["Профиль", "Кол-во"])
        for r in self._by_profile(period):
            ws1.append([r["profile"], r["total"]])
        ws1.append([])

        # По состояниям
        ws1.append(["По состояниям КА"])
        ws1.append(["Состояние", "Кол-во"])
        for r in self._by_state(period):
            ws1.append([r["state"], r["total"]])

        for col in range(1, 5):
            ws1.column_dimensions[get_column_letter(col)].width = 28

        # Лист 2: подробности
        ws2 = wb.create_sheet("Подробно")
        details = self._details(period)
        if details:
            headers = list(details[0].keys())
            ws2.append(headers)
            for cell in ws2[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="DDDDDD")
            for r in details:
                ws2.append([str(r[k]) if r[k] is not None else "" for k in headers])
            for col in range(1, len(headers) + 1):
                ws2.column_dimensions[get_column_letter(col)].width = 18
        else:
            ws2.append(["Нет данных за период"])

        wb.save(path)
        self._register(period, path, "XLSX", total)
        return path

    # --------------------------------------------------------------- PDF

    def generate_pdf(self, period: ReportPeriod) -> Path:
        path = self.out_dir / f"defect_report_{period.period_to:%Y%m%d_%H%M%S}.pdf"
        doc = SimpleDocTemplate(str(path), pagesize=A4,
                                topMargin=36, bottomMargin=36,
                                leftMargin=36, rightMargin=36)
        styles = getSampleStyleSheet()
        title = ParagraphStyle(
            "TitleRu", parent=styles["Title"], fontName=self.font, fontSize=16,
            spaceAfter=12,
        )
        h2 = ParagraphStyle(
            "H2Ru", parent=styles["Heading2"], fontName=self.font, fontSize=12,
            spaceBefore=10, spaceAfter=6,
        )
        body = ParagraphStyle(
            "BodyRu", parent=styles["BodyText"], fontName=self.font, fontSize=10,
        )

        story = [
            Paragraph("Отчёт о дефектах производства световых конструкций", title),
            Paragraph(REPORTS.company, body),
            Paragraph(
                f"Период: {period.period_from:%Y-%m-%d %H:%M} — "
                f"{period.period_to:%Y-%m-%d %H:%M}", body,
            ),
            Spacer(1, 12),
        ]

        # Сводка по типам
        by_type = self._by_type(period)
        total = sum(int(r["total"]) for r in by_type)
        story.append(Paragraph("1. Распределение дефектов по типам", h2))
        data = [["Код", "Название", "Кол-во"]] + [
            [r["defect_code"], r["defect_name"], r["total"]] for r in by_type
        ] + [["", "Итого", total]]
        story.append(self._make_table(data))
        story.append(Spacer(1, 12))

        # По профилям
        story.append(Paragraph("2. Распределение по профилям", h2))
        data = [["Профиль", "Кол-во"]] + [
            [r["profile"], r["total"]] for r in self._by_profile(period)
        ]
        story.append(self._make_table(data))
        story.append(Spacer(1, 12))

        # По состояниям КА
        story.append(Paragraph("3. Распределение по состояниям конечного автомата", h2))
        data = [["Состояние", "Кол-во"]] + [
            [r["state"], r["total"]] for r in self._by_state(period)
        ]
        story.append(self._make_table(data))
        story.append(Spacer(1, 12))

        # Детали (10 последних)
        details = self._details(period, limit=10)
        if details:
            story.append(Paragraph("4. Последние зафиксированные дефекты", h2))
            headers = ["Дата", "Заказ/№", "Профиль", "Код", "Состояние", "Знач.", "Цель", "Порог"]
            rows = [headers]
            for r in details:
                rows.append([
                    str(r["detected_at"])[:19],
                    f"{r['order_id']}/{r['unit_index']}",
                    r["profile"],
                    r["defect_code"],
                    r["detected_state"],
                    f"{r['measured_value']:.3f}" if r["measured_value"] is not None else "",
                    f"{r['target_value']:.3f}" if r.get("target_value") is not None else "",
                    f"{r['threshold_value']:.3f}" if r["threshold_value"] is not None else "",
                ])
            story.append(self._make_table(rows, font_size=8))

        doc.build(story)
        self._register(period, path, "PDF", total)
        return path

    def _make_table(self, data, font_size: int = 9) -> Table:
        t = Table(data, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), self.font, font_size),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#01696F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#999999")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F5F5F2")]),
            ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return t
