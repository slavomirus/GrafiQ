from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape, portrait
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
from datetime import datetime, date, time, timedelta
from typing import List, Dict, Any, Optional
from bson import ObjectId
import os
import logging
import calendar

logger = logging.getLogger(__name__)

# --- Rejestracja Czcionek ze wsparciem polskich znaków ---
FONT_NAME = "DejaVuSans"
FONT_NAME_BOLD = "DejaVuSans-Bold"
FONT_REGISTERED = False

def register_polish_font():
    """Rejestruje fonty TTF obsługujące polskie znaki diakrytyczne."""
    global FONT_REGISTERED, FONT_NAME, FONT_NAME_BOLD
    if FONT_REGISTERED:
        return

    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        font_path = os.path.join(base_dir, "static", "fonts", "DejaVuSans.ttf")
        font_path_bold = os.path.join(base_dir, "static", "fonts", "DejaVuSans-Bold.ttf")

        system_fonts_regular = [
            font_path,
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
        ]
        
        system_fonts_bold = [
            font_path_bold,
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc", 
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        ]

        found_reg = next((p for p in system_fonts_regular if os.path.exists(p)), None)
        found_bold = next((p for p in system_fonts_bold if os.path.exists(p)), None)
        
        if found_reg and found_bold:
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, found_reg))
                pdfmetrics.registerFont(TTFont(FONT_NAME_BOLD, found_bold))
                logger.info(f"Zarejestrowano czcionki TTF: {found_reg}, {found_bold}")
            except Exception as fe:
                logger.warning(f"Błąd rejestracji TTF ({fe}), fallback do standardowego Helvetica")
                FONT_NAME = "Helvetica"
                FONT_NAME_BOLD = "Helvetica-Bold"
        else:
            FONT_NAME = "Helvetica"
            FONT_NAME_BOLD = "Helvetica-Bold"
            logger.warning("Nie znaleziono TTF. Fallback do wbudowanego Helvetica.")

        FONT_REGISTERED = True
    except Exception as e:
        logger.error(f"Nie udało się zarejestrować czcionki: {e}", exc_info=True)
        FONT_NAME = "Helvetica"
        FONT_NAME_BOLD = "Helvetica-Bold"
        FONT_REGISTERED = True

register_polish_font()

PL_WEEKDAYS = ['PN', 'WT', 'ŚR', 'CZ', 'PT', 'SB', 'ND']
PL_MONTHS = {
    1: "Styczeń", 2: "Luty", 3: "Marzec", 4: "Kwiecień", 5: "Maj", 6: "Czerwiec",
    7: "Lipiec", 8: "Sierpień", 9: "Wrzesień", 10: "Październik", 11: "Listopad", 12: "Grudzień"
}

def parse_time(t: Any) -> Optional[time]:
    if isinstance(t, time): return t
    if isinstance(t, datetime): return t.time()
    if isinstance(t, str):
        t_clean = t.strip()
        try: return datetime.strptime(t_clean, "%H:%M").time()
        except ValueError:
            try: return datetime.strptime(t_clean, "%H:%M:%S").time()
            except ValueError: return None
    return None

def calculate_hours(start: time, end: time) -> float:
    if not start or not end: return 0.0
    d = date(2000, 1, 1)
    dt_start = datetime.combine(d, start)
    dt_end = datetime.combine(d, end)
    if dt_end < dt_start: dt_end += timedelta(days=1)
    return (dt_end - dt_start).total_seconds() / 3600.0

def safe_to_date(val: Any, default: Optional[date] = None) -> date:
    if isinstance(val, datetime): return val.date()
    if isinstance(val, date): return val
    if isinstance(val, str):
        try:
            clean = val.replace("Z", "+00:00").split("T")[0]
            return datetime.strptime(clean, "%Y-%m-%d").date()
        except Exception:
            pass
    return default or date.today()

def matches_employee(emp_field: Any, target_emp_id: Any) -> bool:
    if not emp_field: return False
    target_str = str(target_emp_id)
    if isinstance(emp_field, (str, ObjectId)):
        return str(emp_field) == target_str
    if isinstance(emp_field, dict):
        e_id = str(emp_field.get("id") or emp_field.get("_id") or emp_field.get("user_id") or "")
        return e_id == target_str
    return False

def is_on_sick_leave(user_id: str, check_date: date, sick_leaves: List[dict]) -> bool:
    if not sick_leaves: return False
    check_dt = datetime.combine(check_date, time.min)
    u_str = str(user_id)
    for leave in sick_leaves:
        if str(leave.get("user_id")) != u_str: continue
        start = safe_to_date(leave.get("start_date"))
        end = safe_to_date(leave.get("end_date"))
        if start <= check_date <= end:
            return True
    return False

def is_on_vacation(user_id: str, check_date: date, vacations: List[dict]) -> bool:
    if not vacations: return False
    u_str = str(user_id)
    for vac in vacations:
        if str(vac.get("user_id")) != u_str: continue
        status = vac.get("status")
        if status and status != "approved": continue
        start = safe_to_date(vac.get("start_date"))
        end = safe_to_date(vac.get("end_date"))
        if start <= check_date <= end:
            return True
    return False

# =========================================================================
# 1. RAPORT GODZINOWY DLA PRACOWNIKA (PIONOWA KARTA CZASU PRACY, BEZ WYPŁATY)
# =========================================================================
def generate_employee_hours_report_pdf(
    report_data: List[Dict[str, Any]], 
    user_info: dict, 
    start_date: date, 
    end_date: date, 
    sick_leaves: List[dict] = None, 
    vacations: List[dict] = None
) -> BytesIO:
    """
    Generuje elegancką kartę ewidencji czasu pracy dla pracownika w formacie A4 Portrait.
    Uwzględnia czas pracy, dni robocze, L4 i urlopy - BEZ stawek i prognozowanej wypłaty.
    """
    if sick_leaves is None: sick_leaves = []
    if vacations is None: vacations = []

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    elements = []

    styles = getSampleStyleSheet()
    styles['Normal'].fontName = FONT_NAME
    styles['Title'].fontName = FONT_NAME_BOLD
    styles['Heading2'].fontName = FONT_NAME_BOLD

    # Nagłówek dokumentu
    elements.append(Paragraph("KARTA EWIDENCJI CZASU PRACY", styles['Title']))
    elements.append(Spacer(1, 4))
    
    fn = user_info.get("first_name", "")
    ln = user_info.get("last_name", "")
    full_name = f"{fn} {ln}".strip() or "Pracownik"
    role_label = user_info.get("role", "Pracownik").capitalize()
    franchise_code = user_info.get("franchise_code", "Sklep")
    
    meta_text = (
        f"<b>Pracownik:</b> {full_name} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"<b>Stanowisko:</b> {role_label} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"<b>Sklep:</b> {franchise_code}<br/>"
        f"<b>Okres rozliczeniowy:</b> {start_date.strftime('%d.%m.%Y')} – {end_date.strftime('%d.%m.%Y')}"
    )
    elements.append(Paragraph(meta_text, styles['Normal']))
    elements.append(Spacer(1, 12))

    # Zbuduj mapę zmian po dacie
    user_id_str = str(user_info.get("_id") or user_info.get("id") or "")
    shifts_by_date: Dict[str, List[Dict[str, Any]]] = {}
    
    for entry in report_data:
        e_user = str(entry.get("user_id") or "")
        if e_user and e_user != user_id_str:
            continue
        d_val = safe_to_date(entry.get("date"))
        d_str = d_val.strftime("%Y-%m-%d")
        if d_str not in shifts_by_date:
            shifts_by_date[d_str] = []
        shifts_by_date[d_str].append(entry)

    # Budujemy tabelę dni
    table_data = [
        ["Data", "Dzień", "Rozpoczęcie", "Zakończenie", "Czas pracy", "Uwagi / Zmiana"]
    ]

    total_worked_hours = 0.0
    working_days_count = 0
    l4_days_count = 0
    vacation_days_count = 0

    curr = start_date
    while curr <= end_date:
        d_str = curr.strftime("%Y-%m-%d")
        weekday_name = PL_WEEKDAYS[curr.weekday()]
        day_label = curr.strftime("%d.%m")
        
        row_start = "-"
        row_end = "-"
        row_hours = "-"
        row_notes = ""

        if is_on_sick_leave(user_id_str, curr, sick_leaves):
            row_notes = "Zwolnienie lekarskie (L4)"
            l4_days_count += 1
        elif is_on_vacation(user_id_str, curr, vacations):
            row_notes = "Urlop wypoczynkowy (UW)"
            vacation_days_count += 1
        else:
            entries = shifts_by_date.get(d_str, [])
            if entries:
                day_hours = 0.0
                start_parts = []
                end_parts = []
                notes_parts = []

                for e in entries:
                    s_t = parse_time(e.get("start_time"))
                    e_t = parse_time(e.get("end_time"))
                    h = e.get("hours")
                    if h is None and s_t and e_t:
                        h = calculate_hours(s_t, e_t)
                    day_hours += float(h or 0.0)

                    if s_t: start_parts.append(s_t.strftime("%H:%M"))
                    if e_t: end_parts.append(e_t.strftime("%H:%M"))
                    
                    s_name = e.get("shift_name") or ""
                    if s_name:
                        s_lower = s_name.lower()
                        if s_lower in ["morning", "r"]: s_name = "Rano (R)"
                        elif s_lower in ["mid", "middle", "m"]: s_name = "Środek (M)"
                        elif s_lower in ["closing", "z"]: s_name = "Zamknięcie (Z)"
                        elif s_lower.startswith("custom") or s_lower == "w": s_name = "Indywidualna (W)"
                        notes_parts.append(s_name)

                row_start = ", ".join(start_parts) if start_parts else "-"
                row_end = ", ".join(end_parts) if end_parts else "-"
                row_hours = f"{day_hours:.1f} h"
                row_notes = ", ".join(notes_parts)

                total_worked_hours += day_hours
                working_days_count += 1
            else:
                row_notes = "Dzień wolny"

        table_data.append([day_label, weekday_name, row_start, row_end, row_hours, row_notes])
        curr += timedelta(days=1)

    # Wiersz podsumowania na dole tabeli
    table_data.append([
        "SUMA:", "", "", "", f"{total_worked_hours:.1f} h", f"{working_days_count} dni roboczych"
    ])

    # Szerokości kolumn (dostępna szerokość: 545 punktów A4 Portrait)
    col_widths = [55, 45, 80, 80, 85, 200]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    style_cmds = [
        ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D1D5DB')),
        ('ALIGN', (0, 0), (1, -1), 'CENTER'),
        ('ALIGN', (2, 0), (4, -1), 'CENTER'),
        ('ALIGN', (5, 0), (5, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#003057')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), FONT_NAME_BOLD),
        ('FONTSIZE', (0, 0), (-1, 0), 8.5),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#F3F4F6')),
        ('FONTNAME', (0, -1), (-1, -1), FONT_NAME_BOLD),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
    ]

    for r_idx, row in enumerate(table_data[1:-1], start=1):
        note = row[5]
        if "L4" in note:
            style_cmds.append(('BACKGROUND', (0, r_idx), (-1, r_idx), colors.HexColor('#FEE2E2')))
            style_cmds.append(('TEXTCOLOR', (5, r_idx), (5, r_idx), colors.HexColor('#991B1B')))
        elif "Urlop" in note:
            style_cmds.append(('BACKGROUND', (0, r_idx), (-1, r_idx), colors.HexColor('#FEF3C7')))
            style_cmds.append(('TEXTCOLOR', (5, r_idx), (5, r_idx), colors.HexColor('#92400E')))
        elif row[1] in ['SB', 'ND']:
            style_cmds.append(('BACKGROUND', (0, r_idx), (1, r_idx), colors.HexColor('#F9FAFB')))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)
    elements.append(Spacer(1, 14))

    # Podsumowanie liczbowe
    summary_html = (
        f"<b>Podsumowanie okresu:</b><br/>"
        f"• Przepracowane godziny: <b>{total_worked_hours:.1f} godz.</b><br/>"
        f"• Liczba przepracowanych dni: <b>{working_days_count} dni</b><br/>"
        f"• Liczba dni zwolnienia lekarskiego (L4): <b>{l4_days_count} dni</b><br/>"
        f"• Liczba dni urlopu wypoczynkowego: <b>{vacation_days_count} dni</b>"
    )
    elements.append(Paragraph(summary_html, styles['Normal']))
    elements.append(Spacer(1, 24))

    # Podpisy
    sig_data = [
        ["...................................................", "..................................................."],
        ["Podpis pracownika", "Podpis pracodawcy / franczyzobiorcy"]
    ]
    sig_table = Table(sig_data, colWidths=[270, 275])
    sig_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TEXTCOLOR', (0, 1), (-1, 1), colors.HexColor('#4B5563')),
    ]))
    elements.append(KeepTogether(sig_table))

    doc.build(elements)
    buffer.seek(0)
    return buffer

# =========================================================================
# 2. RAPORT GODZINOWY DLA FRANCZYZOBIORCY (TABELA MACIERZOWA: DNI W WIERSZACH, PRACOWNICY W KOLUMNACH)
# =========================================================================
def generate_franchisee_matrix_hours_report_pdf(
    report_data: List[Dict[str, Any]], 
    all_employees: List[dict], 
    start_date: date, 
    end_date: date, 
    sick_leaves: List[dict] = None, 
    vacations: List[dict] = None,
    franchise_code: str = "Sklep"
) -> BytesIO:
    """
    Generuje pełny raport godzinowy dla franczyzobiorcy:
    Zestawienie wszystkich pracowników w jednej tabeli - RZĘDY DNI i KOLUMNY PRACOWNIKÓW.
    """
    if sick_leaves is None: sick_leaves = []
    if vacations is None: vacations = []

    buffer = BytesIO()
    
    # Sortowanie pracowników
    sorted_employees = sorted(all_employees, key=lambda x: (x.get('last_name', ''), x.get('first_name', '')))
    num_emps = len(sorted_employees)

    # Wybór orientacji: jeśli dużo pracowników (> 6), landscape, inaczej portrait
    use_landscape = num_emps > 5
    page_size = landscape(A4) if use_landscape else portrait(A4)
    page_width = page_size[0]
    
    margin = 15
    doc = SimpleDocTemplate(buffer, pagesize=page_size, rightMargin=margin, leftMargin=margin, topMargin=18, bottomMargin=18)
    elements = []

    styles = getSampleStyleSheet()
    styles['Normal'].fontName = FONT_NAME
    styles['Title'].fontName = FONT_NAME_BOLD

    title = f"Zestawienie Czasu Pracy Pracowników – {franchise_code}"
    elements.append(Paragraph(title, styles['Title']))
    elements.append(Spacer(1, 4))
    
    elements.append(Paragraph(f"Okres: <b>{start_date.strftime('%d.%m.%Y')} – {end_date.strftime('%d.%m.%Y')}</b>", styles['Normal']))
    elements.append(Spacer(1, 10))

    # Przygotowanie nagłówka
    header_row = ["Dzień"]
    emp_ids = []
    emp_totals = {}

    for emp in sorted_employees:
        fn = emp.get("first_name", "")
        ln = emp.get("last_name", "")
        short_name = f"{fn} {ln[:1]}." if ln else fn
        header_row.append(short_name)
        eid = str(emp.get("_id") or emp.get("id"))
        emp_ids.append(eid)
        emp_totals[eid] = 0.0

    # Zmapuj raport po dacie i user_id
    data_by_date_user: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for entry in report_data:
        d_val = safe_to_date(entry.get("date"))
        d_str = d_val.strftime("%Y-%m-%d")
        u_id = str(entry.get("user_id") or "")
        if d_str not in data_by_date_user:
            data_by_date_user[d_str] = {}
        if u_id not in data_by_date_user[d_str]:
            data_by_date_user[d_str][u_id] = []
        data_by_date_user[d_str][u_id].append(entry)

    table_data = [header_row]

    curr = start_date
    while curr <= end_date:
        d_str = curr.strftime("%Y-%m-%d")
        w_day = PL_WEEKDAYS[curr.weekday()]
        row = [f"{curr.day:02d} {w_day}"]

        for eid in emp_ids:
            cell_text = ""
            if is_on_sick_leave(eid, curr, sick_leaves):
                cell_text = "L4"
            elif is_on_vacation(eid, curr, vacations):
                cell_text = "Urlop"
            else:
                entries = data_by_date_user.get(d_str, {}).get(eid, [])
                if entries:
                    parts = []
                    for e in entries:
                        s_t = parse_time(e.get("start_time"))
                        e_t = parse_time(e.get("end_time"))
                        h = e.get("hours")
                        if h is None and s_t and e_t:
                            h = calculate_hours(s_t, e_t)
                        h_val = float(h or 0.0)
                        emp_totals[eid] += h_val

                        if s_t and e_t:
                            parts.append(f"{s_t.strftime('%H:%M')}-{e_t.strftime('%H:%M')}")
                        else:
                            parts.append(f"{h_val:.1f}h")
                    cell_text = "\n".join(parts)

            row.append(cell_text)

        table_data.append(row)
        curr += timedelta(days=1)

    # Wiersz podsumowania na dole
    summary_row = ["Suma:"]
    for eid in emp_ids:
        summary_row.append(f"{emp_totals[eid]:.1f}h")
    table_data.append(summary_row)

    # Obliczanie szerokości kolumn tak, aby NIGDY nie przekroczyć szerokości strony
    available_width = page_width - (2 * margin) - 4
    day_col_width = 46.0
    rem_width = available_width - day_col_width
    emp_col_width = rem_width / num_emps if num_emps > 0 else 60.0

    col_widths = [day_col_width] + [emp_col_width] * num_emps
    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    font_sz = 7 if num_emps > 8 else 7.5

    style_cmds = [
        ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
        ('FONTSIZE', (0, 0), (-1, -1), font_sz),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#9CA3AF')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#003057')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), FONT_NAME_BOLD),
        ('BACKGROUND', (0, 1), (0, -2), colors.HexColor('#F3F4F6')),
        ('FONTNAME', (0, 1), (0, -2), FONT_NAME_BOLD),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#E5E7EB')),
        ('FONTNAME', (0, -1), (-1, -1), FONT_NAME_BOLD),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
    ]

    for r_idx, row in enumerate(table_data[1:-1], start=1):
        for c_idx, cell in enumerate(row[1:], start=1):
            if cell == "L4":
                style_cmds.append(('BACKGROUND', (c_idx, r_idx), (c_idx, r_idx), colors.HexColor('#FEE2E2')))
                style_cmds.append(('TEXTCOLOR', (c_idx, r_idx), (c_idx, r_idx), colors.HexColor('#991B1B')))
            elif cell == "Urlop":
                style_cmds.append(('BACKGROUND', (c_idx, r_idx), (c_idx, r_idx), colors.HexColor('#FEF3C7')))
                style_cmds.append(('TEXTCOLOR', (c_idx, r_idx), (c_idx, r_idx), colors.HexColor('#92400E')))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    doc.build(elements)
    buffer.seek(0)
    return buffer

# Kompatybilny wrapper dla starego wywołania
def generate_hours_report_pdf(
    report_data: List[Dict[str, Any]], 
    start_date: date, 
    end_date: date, 
    sick_leaves: List[dict] = None
) -> BytesIO:
    emp_map = {}
    for r in report_data:
        uid = str(r.get("user_id") or "")
        if uid and uid not in emp_map:
            emp_map[uid] = {
                "_id": uid,
                "first_name": r.get("first_name", ""),
                "last_name": r.get("last_name", "")
            }
    all_emps = list(emp_map.values())
    return generate_franchisee_matrix_hours_report_pdf(
        report_data=report_data,
        all_employees=all_emps,
        start_date=start_date,
        end_date=end_date,
        sick_leaves=sick_leaves or []
    )

# =========================================================================
# 3. DRUK WNIOSKU O URLOP WYPOCZYNKOWY DLA PRACOWNIKA / PRACODAWCY
# =========================================================================
def generate_vacation_request_pdf(user: dict, vacation: dict) -> BytesIO:
    """Generuje formalny, estetyczny wniosek urlopowy w formacie PDF (A4 Portrait)."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=35, leftMargin=35, topMargin=35, bottomMargin=35)
    elements = []

    styles = getSampleStyleSheet()
    styles['Normal'].fontName = FONT_NAME
    styles['Title'].fontName = FONT_NAME_BOLD
    styles['Heading2'].fontName = FONT_NAME_BOLD

    # Nagłówek z datą po prawej stronie
    now_str = datetime.now().strftime("%d.%m.%Y")
    header_table = Table(
        [[
            Paragraph(f"<b>Miejscowość:</b> {user.get('franchise_code', 'Sklep')}", styles['Normal']),
            Paragraph(f"<b>Data:</b> {now_str}", styles['Normal'])
        ]],
        colWidths=[260, 265]
    )
    header_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 20))

    # Dane pracownika
    fn = user.get("first_name", "")
    ln = user.get("last_name", "")
    full_name = f"{fn} {ln}".strip() or "Pracownik"
    role = user.get("role", "Pracownik").capitalize()
    
    emp_info = (
        f"<b>Imię i nazwisko:</b> {full_name}<br/>"
        f"<b>Stanowisko:</b> {role}<br/>"
        f"<b>Jednostka / Sklep:</b> {user.get('franchise_code', 'Sklep')}"
    )
    elements.append(Paragraph(emp_info, styles['Normal']))
    elements.append(Spacer(1, 25))

    # Tytuł
    elements.append(Paragraph("WNIOSEK O URLOP WYPOCZYNKOWY", styles['Title']))
    elements.append(Spacer(1, 15))

    # Treść wniosku
    v_start = safe_to_date(vacation.get('start_date'))
    v_end = safe_to_date(vacation.get('end_date'))
    days_req = vacation.get('days_requested') or 0
    status = vacation.get('status', 'pending')
    status_label = "ZATWIERDZONY" if status == "approved" else "ODRZUCONY" if status == "rejected" else "OCZEKUJĄCY NA DECYZJĘ"

    body_text = (
        f"Zwracam się z uprzejmą prośbą o udzielenie mi urlopu wypoczynkowego "
        f"w okresie od dnia <b>{v_start.strftime('%d.%m.%Y')}</b> "
        f"do dnia <b>{v_end.strftime('%d.%m.%Y')}</b> włącznie.<br/><br/>"
        f"Łączny wymiar wnioskowanego urlopu: <b>{days_req} dni roboczych</b>."
    )
    elements.append(Paragraph(body_text, styles['Normal']))
    elements.append(Spacer(1, 15))

    if vacation.get('reason'):
        elements.append(Paragraph(f"<b>Uzasadnienie:</b> {vacation['reason']}", styles['Normal']))
        elements.append(Spacer(1, 15))

    # Ramka statusu
    status_box = Table(
        [[Paragraph(f"<b>STATUS WNIOSKU:</b> {status_label}", styles['Normal'])]],
        colWidths=[525]
    )
    bg_color = colors.HexColor('#D1FAE5') if status == "approved" else colors.HexColor('#FEE2E2') if status == "rejected" else colors.HexColor('#EFF6FF')
    status_box.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), bg_color),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#9CA3AF')),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(status_box)
    elements.append(Spacer(1, 45))

    # Podpisy
    sig_data = [
        ["...................................................", "..................................................."],
        ["Podpis pracownika", "Decyzja i podpis pracodawcy"]
    ]
    sig_table = Table(sig_data, colWidths=[260, 265])
    sig_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TEXTCOLOR', (0, 1), (-1, 1), colors.HexColor('#4B5563')),
    ]))
    elements.append(KeepTogether(sig_table))

    doc.build(elements)
    buffer.seek(0)
    return buffer

# =========================================================================
# 4. GRAFIK PRACY (TYGODNIOWY / MIESIĘCZNY WIDOK GRAFIKU DLA SKLEPU)
# =========================================================================
def generate_schedule_pdf(
    schedule_data: dict, 
    store_settings: dict, 
    sick_leaves: List[dict], 
    all_employees: List[dict]
) -> BytesIO:
    """
    Generuje elegancki grafik w orientacji poziomej landscape A4.
    Zabezpieczony przed LayoutError i błędami dopasowania id/ObjectId.
    """
    buffer = BytesIO()
    
    # A4 landscape: 841.89 x 595.27 punktów
    margin = 12
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=landscape(A4), 
        rightMargin=margin, 
        leftMargin=margin, 
        topMargin=15, 
        bottomMargin=15
    )
    elements = []

    styles = getSampleStyleSheet()
    styles['Normal'].fontName = FONT_NAME
    styles['Title'].fontName = FONT_NAME_BOLD

    franchise_code = schedule_data.get('franchise_code', 'Sklep')
    title = f"Grafik Pracy – {franchise_code}"
    elements.append(Paragraph(title, styles['Title']))

    start_date = safe_to_date(schedule_data.get('start_date'))
    end_date = safe_to_date(schedule_data.get('end_date'), default=start_date + timedelta(days=6))

    elements.append(Paragraph(
        f"Okres: <b>{start_date.strftime('%d.%m.%Y')} – {end_date.strftime('%d.%m.%Y')}</b>", 
        styles['Normal']
    ))
    elements.append(Spacer(1, 8))

    dates = []
    curr = start_date
    while curr <= end_date:
        dates.append(curr)
        curr += timedelta(days=1)

    num_days = len(dates)
    header_row = ["Pracownik"]
    for d in dates:
        header_row.append(f"{d.day}\n{PL_WEEKDAYS[d.weekday()]}")
    header_row.append("Suma")

    table_data = [header_row]
    sorted_employees = sorted(all_employees, key=lambda x: (x.get('last_name', ''), x.get('first_name', '')))
    schedule_content = schedule_data.get('schedule', {}) or {}

    for emp in sorted_employees:
        emp_id = str(emp.get('_id') or emp.get('id') or '')
        fn = emp.get('first_name', '')
        ln = emp.get('last_name', '')
        short_name = f"{fn} {ln[:1]}." if ln else fn
        row = [short_name]
        total_hours = 0.0
        l4_days = 0

        for d in dates:
            d_str = d.strftime("%Y-%m-%d")

            if is_on_sick_leave(emp_id, d, sick_leaves):
                row.append("L4")
                l4_days += 1
                continue

            cell_text = ""
            if d_str in schedule_content:
                day_sched = schedule_content[d_str]
                if isinstance(day_sched, dict):
                    if day_sched.get("is_closed"):
                        cell_text = "-"
                    else:
                        for shift_name, shift_data in day_sched.items():
                            if not isinstance(shift_data, dict): continue
                            emps_in_shift = shift_data.get('employees', [])
                            if any(matches_employee(e, emp_id) for e in emps_in_shift):
                                s_time = parse_time(shift_data.get('start_time'))
                                e_time = parse_time(shift_data.get('end_time'))

                                if s_time and e_time:
                                    cell_text = f"{s_time.strftime('%H:%M')}\n{e_time.strftime('%H:%M')}"
                                    total_hours += calculate_hours(s_time, e_time)
                                else:
                                    cell_text = shift_name
                                break

            row.append(cell_text)

        summary_text = f"{total_hours:.1f}h"
        if l4_days > 0: summary_text += f"\n({l4_days} L4)"
        row.append(summary_text)
        table_data.append(row)

    # Obliczenie szerokości kolumn (dostępne: 841.89 - 24 = 817.89 pt)
    available_width = 841.89 - (2 * margin) - 4
    col_emp = 85.0
    col_sum = 45.0
    col_day = (available_width - col_emp - col_sum) / num_days if num_days > 0 else 50.0

    col_widths = [col_emp] + [col_day] * num_days + [col_sum]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    f_size = 6.0 if num_days > 25 else 6.5

    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#003057')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#9CA3AF')),
        ('FONTSIZE', (0, 0), (-1, -1), f_size),
        ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
        ('FONTNAME', (0, 0), (-1, 0), FONT_NAME_BOLD),
        ('FONTNAME', (0, 0), (0, -1), FONT_NAME_BOLD),
        ('LEFTPADDING', (0, 0), (-1, -1), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]

    for r_idx, row in enumerate(table_data[1:], start=1):
        for c_idx, cell in enumerate(row[1:-1], start=1):
            if cell == "L4":
                style_cmds.append(('BACKGROUND', (c_idx, r_idx), (c_idx, r_idx), colors.HexColor('#FEE2E2')))
                style_cmds.append(('TEXTCOLOR', (c_idx, r_idx), (c_idx, r_idx), colors.HexColor('#991B1B')))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_monthly_schedule_pdf(
    month: int, 
    year: int, 
    schedule_data: dict, 
    store_settings: dict, 
    sick_leaves: List[dict], 
    all_employees: List[dict]
) -> BytesIO:
    """Miesięczny grafik z podsumowaniem (Dni w wierszach, pracownicy w kolumnach)."""
    _, last_day = calendar.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)
    
    # Przekierowujemy do uniwersalnego generatora macierzowego
    report_data = []
    sched_content = schedule_data.get('schedule', {}) or {}
    
    for d_str, day_sched in sched_content.items():
        if not isinstance(day_sched, dict) or day_sched.get("is_closed"):
            continue
        try:
            d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
        except:
            continue
            
        for s_name, s_data in day_sched.items():
            if not isinstance(s_data, dict): continue
            s_time = parse_time(s_data.get('start_time'))
            e_time = parse_time(s_data.get('end_time'))
            h = calculate_hours(s_time, e_time) if s_time and e_time else 0.0

            for emp in s_data.get('employees', []):
                emp_id = str(emp.get('id') or emp.get('_id') or '')
                if emp_id:
                    report_data.append({
                        "user_id": emp_id,
                        "date": d_obj,
                        "start_time": s_time,
                        "end_time": e_time,
                        "hours": h,
                        "shift_name": s_name
                    })

    return generate_franchisee_matrix_hours_report_pdf(
        report_data=report_data,
        all_employees=all_employees,
        start_date=start_date,
        end_date=end_date,
        sick_leaves=sick_leaves,
        franchise_code=schedule_data.get('franchise_code', 'Sklep')
    )
