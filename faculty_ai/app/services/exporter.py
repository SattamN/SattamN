"""Gradebook assembly + Excel / CSV / PDF export."""
import statistics
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import get_settings
from ..models import Exam, Submission

# --------------------------------------------------------------------------- gradebook data


def build_gradebook(db: Session, exam: Exam, include_unapproved: bool = False) -> dict:
    """Marks matrix for an exam. Approved papers use final marks only; provisional papers (optional)
    fall back to the AI mark and are labelled as such."""
    questions = sorted(exam.questions, key=lambda q: q.number)
    subs = (
        db.query(Submission)
        .filter(Submission.exam_id == exam.id)
        .options(selectinload(Submission.grades), joinedload(Submission.student))
        .all()
    )
    total_max = round(sum(q.max_mark for q in questions), 4)
    rows = []
    for s in sorted(subs, key=lambda s: s.student.university_id):
        approved = s.status == "approved"
        if not approved and not include_unapproved:
            continue
        by_q = {g.question_id: g for g in s.grades}
        marks: dict[str, Optional[float]] = {}
        for q in questions:
            g = by_q.get(q.id)
            if g is None:
                v = None
            elif approved:
                v = g.final_mark
            else:
                v = g.final_mark if g.final_mark is not None else g.ai_mark
            marks[str(q.number)] = v
        vals = [v for v in marks.values() if v is not None]
        complete = len(vals) == len(questions) and bool(questions)
        total = round(sum(vals), 4) if vals else None
        rows.append(
            {
                "submission_id": s.id,
                "student_id": s.student_id,
                "student_name": s.student.name,
                "university_id": s.student.university_id,
                "status": s.status,
                "provisional": not approved,
                "complete": complete,
                "marks": marks,
                "total": total,
                "percent": round(total / total_max * 100, 2) if (total is not None and total_max) else None,
            }
        )
    totals = [r["total"] for r in rows if r["complete"]]
    stats = {"count": len(totals)}
    if totals:
        stats.update(
            mean=round(statistics.fmean(totals), 2),
            median=round(statistics.median(totals), 2),
            stdev=round(statistics.pstdev(totals), 2),  # population SD
            min=min(totals),
            max=max(totals),
        )
    return {
        "exam": {"id": exam.id, "title": exam.title, "course": exam.course.name, "course_code": exam.course.code},
        "questions": [{"number": q.number, "max_mark": q.max_mark, "clo": q.clo} for q in questions],
        "total_max": total_max,
        "rows": rows,
        "stats": stats,
    }


# --------------------------------------------------------------------------- helpers


def _fname(gb: dict, ext: str) -> Path:
    settings = get_settings()
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return settings.exports_dir / f"exam{gb['exam']['id']}_gradebook_{stamp}.{ext}"


def _table(gb: dict) -> tuple[list[str], list[list]]:
    qs = gb["questions"]
    header = ["University ID", "Student"] + [f"Q{q['number']}" for q in qs] + ["Total", "Percent", "Status"]
    rows = []
    for r in gb["rows"]:
        rows.append(
            [r["university_id"], r["student_name"]]
            + [r["marks"][str(q["number"])] for q in qs]
            + [r["total"], r["percent"], "provisional" if r["provisional"] else "approved"]
        )
    return header, rows


# --------------------------------------------------------------------------- Excel


def export_xlsx(gb: dict) -> Path:
    qs = gb["questions"]
    nq = len(qs)
    first_q, last_q = 3, 2 + nq
    c_total, c_pct, c_status = last_q + 1, last_q + 2, last_q + 3
    fq, lq = get_column_letter(first_q), get_column_letter(last_q)
    tcol = get_column_letter(c_total)

    wb = Workbook()
    ws = wb.active
    ws.title = "Gradebook"
    header, _ = _table(gb)
    ws.append(header)
    ws.append(["", "Max marks"] + [q["max_mark"] for q in qs] + [f"=SUM({fq}2:{lq}2)", "", ""])
    for r in gb["rows"]:
        i = ws.max_row + 1
        marks = [r["marks"][str(q["number"])] for q in qs]
        # live formulas so edits in Excel update totals
        ws.append(
            [r["university_id"], r["student_name"]] + marks
            + [f"=SUM({fq}{i}:{lq}{i})", f'=IF(${tcol}$2=0,"",{tcol}{i}/${tcol}$2)',
               "provisional" if r["provisional"] else "approved"]
        )
        ws.cell(i, c_pct).number_format = "0.0%"
        if r["provisional"]:
            for c in range(1, c_status + 1):
                ws.cell(i, c).font = Font(italic=True, color="808080")

    head_fill = PatternFill("solid", fgColor="1F4E78")
    for c in range(1, c_status + 1):
        cell = ws.cell(1, c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center")
        ws.cell(2, c).font = Font(bold=True)
        ws.cell(2, c).fill = PatternFill("solid", fgColor="DDEBF7")
    ws.freeze_panes = "C3"
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 30
    for c in range(first_q, c_status + 1):
        ws.column_dimensions[get_column_letter(c)].width = 11

    ss = wb.create_sheet("Summary")
    e, st = gb["exam"], gb["stats"]
    for row in [
        ("Course", f"{e.get('course_code') or ''} {e['course']}".strip()),
        ("Exam", e["title"]),
        ("Exported at", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Total marks", gb["total_max"]),
        ("Students (approved & complete)", st.get("count", 0)),
        ("Mean", st.get("mean")),
        ("Median", st.get("median")),
        ("Std. deviation (population)", st.get("stdev")),
        ("Highest", st.get("max")),
        ("Lowest", st.get("min")),
    ]:
        ss.append(row)
    ss.column_dimensions["A"].width = 32
    ss.column_dimensions["B"].width = 36
    for r in range(1, ss.max_row + 1):
        ss.cell(r, 1).font = Font(bold=True)

    path = _fname(gb, "xlsx")
    wb.save(path)
    return path


# --------------------------------------------------------------------------- Excel: editable (bulk re-upload)

EDITABLE_INSTRUCTIONS = (
    "Edit only the white mark cells below. Do not add, remove or reorder rows or columns, and do not "
    "change University ID. Save this file and re-upload it via \"Bulk update from Excel\" — the system "
    "matches rows by University ID, not by row position."
)


def build_editable_xlsx(gb: dict) -> Path:
    """A re-uploadable version of the gradebook: one plain numeric cell per question, no formulas,
    current marks pre-filled (final mark if the paper has one, otherwise the AI mark), sheet-protected
    so only the mark cells can be edited in Excel."""
    from openpyxl.styles import Protection
    from openpyxl.worksheet.datavalidation import DataValidation

    qs = gb["questions"]
    n = len(qs)
    id_col, name_col = 1, 2
    first_q, last_q = 3, 2 + n

    wb = Workbook()
    ws = wb.active
    ws.title = "Gradebook (editable)"

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_q)
    note = ws.cell(1, 1, EDITABLE_INSTRUCTIONS)
    note.font = Font(italic=True, color="8A5A1F")
    note.fill = PatternFill("solid", fgColor="FCEFE0")
    note.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 32

    header = ["University ID", "Student"] + [f"Q{q['number']} (max {q['max_mark']:g})" for q in qs]
    ws.append(header)
    for c in range(1, last_q + 1):
        cell = ws.cell(2, c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    dvs = []
    for q in qs:
        dv = DataValidation(type="decimal", operator="between", formula1=0, formula2=q["max_mark"],
                            error=f"Enter a number between 0 and {q['max_mark']:g} for Q{q['number']}.")
        ws.add_data_validation(dv)
        dvs.append(dv)

    for r in gb["rows"]:
        i = ws.max_row + 1
        marks = [r["marks"][str(q["number"])] for q in qs]
        ws.append([r["university_id"], r["student_name"]] + marks)
        if r["provisional"]:
            for c in (id_col, name_col):
                ws.cell(i, c).font = Font(italic=True, color="808080")
        for qi, q in enumerate(qs):
            cell = ws.cell(i, first_q + qi)
            cell.number_format = "0.##"
            dvs[qi].add(cell)

    # protect everything except the mark cells
    for row in ws.iter_rows(min_row=1, max_row=2, max_col=last_q):
        for cell in row:
            cell.protection = Protection(locked=True)
    for row in ws.iter_rows(min_row=3, max_row=ws.max_row):
        for cell in row:
            cell.protection = Protection(locked=(cell.column <= name_col))
    ws.protection.sheet = True
    ws.protection.formatCells = False
    ws.protection.selectLockedCells = False
    ws.protection.selectUnlockedCells = False

    ws.freeze_panes = "C3"
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 28
    for c in range(first_q, last_q + 1):
        ws.column_dimensions[get_column_letter(c)].width = 14

    settings = get_settings()
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = settings.exports_dir / f"exam{gb['exam']['id']}_editable_{stamp}.xlsx"
    wb.save(path)
    return path


# --------------------------------------------------------------------------- CSV


def export_csv(gb: dict) -> Path:
    import pandas as pd

    header, rows = _table(gb)
    path = _fname(gb, "csv")
    pd.DataFrame(rows, columns=header).to_csv(path, index=False, encoding="utf-8-sig")  # BOM => Excel reads Arabic
    return path


# --------------------------------------------------------------------------- PDF (Arabic-safe)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
]


def _pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    settings = get_settings()
    for cand in ([settings.pdf_font_path] if settings.pdf_font_path else []) + _FONT_CANDIDATES:
        if cand and Path(cand).exists():
            try:
                pdfmetrics.registerFont(TTFont("FacultyFont", cand))
                return "FacultyFont"
            except Exception:
                continue
    return "Helvetica"  # Arabic glyphs will not render; set PDF_FONT_PATH


def _shape(text) -> str:
    """Reshape + reorder Arabic so reportlab renders it correctly."""
    text = "" if text is None else str(text)
    if not any("\u0600" <= ch <= "\u06ff" for ch in text):
        return text
    import arabic_reshaper

    try:
        from bidi.algorithm import get_display
    except ImportError:  # newer python-bidi
        from bidi import get_display
    return get_display(arabic_reshaper.reshape(text))


def export_pdf(gb: dict) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _pdf_font()
    path = _fname(gb, "pdf")
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    h1 = ParagraphStyle("h1", fontName=font, fontSize=15, leading=20)
    body = ParagraphStyle("b", fontName=font, fontSize=9, leading=13)
    e, st = gb["exam"], gb["stats"]

    story = [
        Paragraph(_shape(f"{e['title']}"), h1),
        Paragraph(_shape(f"{e.get('course_code') or ''} {e['course']}  |  Total: {gb['total_max']:g}  |  "
                         f"Exported {datetime.now():%Y-%m-%d %H:%M}"), body),
        Spacer(1, 4 * mm),
    ]
    header, rows = _table(gb)
    data = [[_shape(h) for h in header]]
    data.append(["", "Max"] + [f"{q['max_mark']:g}" for q in gb["questions"]] + [f"{gb['total_max']:g}", "100", ""])
    for r in rows:
        data.append([_shape(c) if isinstance(c, str) else ("" if c is None else f"{c:g}") for c in r])

    n = len(header)
    fixed = [26 * mm, 58 * mm, 22 * mm, 20 * mm, 24 * mm]
    avail = landscape(A4)[0] - 24 * mm - sum(fixed)
    qw = max(9 * mm, avail / max(1, len(gb["questions"])))
    widths = [fixed[0], fixed[1]] + [qw] * len(gb["questions"]) + fixed[2:]
    fs = 8 if n <= 14 else 6.5
    tbl = Table(data, colWidths=widths, repeatRows=2)
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), fs),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#DDEBF7")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, colors.HexColor("#F5F8FB")]),
    ]))
    story.append(tbl)
    if st.get("count"):
        story += [Spacer(1, 5 * mm), Paragraph(
            f"n = {st['count']}   Mean {st['mean']}   Median {st['median']}   SD {st['stdev']}   "
            f"Max {st['max']}   Min {st['min']}", body)]
    doc.build(story)
    return path
