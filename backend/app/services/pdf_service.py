from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape, portrait
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
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

# --- Font Registration ---
FONT_NAME = "DejaVuSans"
FONT_NAME_BOLD = "DejaVuSans-Bold"
FONT_REGISTERED = False

def register_polish_font():
    """Registers a TTF font that supports Polish characters."""
    global FONT_REGISTERED
    if FONT_REGISTERED:
        return

    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        font_path = os.path.join(base_dir, "static", "fonts", "DejaVuSans.ttf")
        font_path_bold = os.path.join(base_dir, "static", "fonts", "DejaVuSans-Bold.ttf")

        system_fonts_regular = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
        ]
        
        system_fonts_bold = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc", 
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        ]

        if not os.path.exists(font_path) or not os.path.exists(font_path_bold):
            logger.warning("Local font files not found. Checking system fonts...")
            found_reg = next((p for p in system_fonts_regular if os.path.exists(p)), None)
            found_bold = next((p for p in system_fonts_bold if os.path.exists(p)), None)
            
            if found_reg and found_bold:
                 pdfmetrics.registerFont(TTFont(FONT_NAME, found_reg))
                 pdfmetrics.registerFont(TTFont(FONT_NAME_BOLD, found_bold))
            else:
                try:
                    pdfmetrics.registerFont(TTFont(FONT_NAME, "Helvetica"))
                    pdfmetrics.registerFont(TTFont(FONT_NAME_BOLD, "Helvetica-Bold"))
                except:
                    pass 
        else:
            pdfmetrics.registerFont(TTFont(FONT_NAME, font_path))
            pdfmetrics.registerFont(TTFont(FONT_NAME_BOLD, font_path_bold))

        FONT_REGISTERED = True
    except Exception as e:
        logger.error(f"Failed to register font: {e}", exc_info=True)
        if not FONT_REGISTERED:
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, "Helvetica"))
                pdfmetrics.registerFont(TTFont(FONT_NAME_BOLD, "Helvetica-Bold"))
            except:
                pass
            FONT_REGISTERED = True

register_polish_font()

def parse_time(t: Any) -> Optional[time]:
    if isinstance(t, time): return t
    if isinstance(t, datetime): return t.time()
    if isinstance(t, str):
        try: return datetime.strptime(t, "%H:%M").time()
        except ValueError:
            try: return datetime.strptime(t, "%H:%M:%S").time()
            except ValueError: return None
    return None

def calculate_hours(start: time, end: time) -> float:
    if not start or not end: return 0.0
    d = date(2000, 1, 1)
    dt_start = datetime.combine(d, start)
    dt_end = datetime.combine(d, end)
    if dt_end < dt_start: dt_end += timedelta(days=1)
    return (dt_end - dt_start).total_seconds() / 3600.0

def is_on_sick_leave(user_id: str, check_date: date, sick_leaves: List[dict]) -> bool:
    check_dt = datetime.combine(check_date, time.min)
    for leave in sick_leaves:
        if str(leave.get("user_id")) != user_id: continue
        start = leave.get("start_date")
        end = leave.get("end_date")
        if isinstance(start, str): start = datetime.fromisoformat(start)
        if isinstance(end, str): end = datetime.fromisoformat(end)
        if isinstance(start, datetime): start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if isinstance(end, datetime): end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        if start <= check_dt <= end: return True
    return False

def generate_schedule_pdf(schedule_data: dict, store_settings: dict, sick_leaves: List[dict], all_employees: List[dict]) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=10, leftMargin=10, topMargin=10, bottomMargin=10)
    elements = []
    
    styles = getSampleStyleSheet()
    styles['Normal'].fontName = FONT_NAME
    styles['Title'].fontName = FONT_NAME_BOLD
    styles['Heading1'].fontName = FONT_NAME_BOLD
    styles['Heading2'].fontName = FONT_NAME_BOLD
    
    franchise_code = schedule_data.get('franchise_code', 'Sklep')
    title = f"Grafik Pracy - {franchise_code}"
    elements.append(Paragraph(title, styles['Title']))
    
    start_date = schedule_data.get('start_date')
    end_date = schedule_data.get('end_date')
    if isinstance(start_date, str): start_date = datetime.fromisoformat(start_date).date()
    elif isinstance(start_date, datetime): start_date = start_date.date()
    if isinstance(end_date, str): end_date = datetime.fromisoformat(end_date).date()
    elif isinstance(end_date, datetime): end_date = end_date.date()

    elements.append(Paragraph(f"Okres: {start_date} - {end_date}", styles['Normal']))
    elements.append(Spacer(1, 10))

    dates = []
    curr = start_date
    while curr <= end_date:
        dates.append(curr)
        curr += timedelta(days=1)
        
    header_row = ["Pracownik"]
    for d in dates:
        header_row.append(str(d.day))
    header_row.append("Suma")
    
    table_data = [header_row]
    sorted_employees = sorted(all_employees, key=lambda x: (x.get('last_name', ''), x.get('first_name', '')))
    schedule_content = schedule_data.get('schedule', {})
    
    for emp in sorted_employees:
        emp_id = str(emp.get('_id'))
        row = [f"{emp.get('first_name', '')} {emp.get('last_name', '')}"]
        total_hours = 0.0
        l4_days = 0
        
        for d in dates:
            date_str = d.strftime("%Y-%m-%d")
            
            if is_on_sick_leave(emp_id, d, sick_leaves):
                row.append("L4")
                l4_days += 1
                continue
            
            cell_text = ""
            
            if date_str in schedule_content:
                day_schedule = schedule_content[date_str]
                if day_schedule.get("is_closed"):
                    # Sklep zamknięty, pracownicy nie mają zmian
                    pass
                else:
                    for shift_name, shift_data in day_schedule.items():
                        if not isinstance(shift_data, dict): continue
                        if any(str(e.get('id')) == emp_id for e in shift_data.get('employees', [])):
                            s_time = parse_time(shift_data.get('start_time'))
                            e_time = parse_time(shift_data.get('end_time'))
                            
                            if s_time and e_time:
                                cell_text = f"{s_time.strftime('%H:%M')}\n-\n{e_time.strftime('%H:%M')}"
                                total_hours += calculate_hours(s_time, e_time)
                            else:
                                cell_text = shift_name # Fallback bezpieczny, ale backend powinien już wstrzyknąć
                            break
            
            row.append(cell_text)
            
        summary_text = f"{total_hours:.1f}h"
        if l4_days > 0: summary_text += f"\n({l4_days} dni L4)"
        row.append(summary_text)
        table_data.append(row)

    available_width = 840 - 160
    num_days = len(dates)
    day_col_width = available_width / num_days if num_days > 0 else 50
    table = Table(table_data, colWidths=[100] + [day_col_width] * num_days + [60])
    
    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('FONTSIZE', (0, 0), (-1, -1), 6.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
        ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
    ]
    for r_idx, row in enumerate(table_data):
        for c_idx, cell in enumerate(row):
            if cell == "L4": style_cmds.append(('BACKGROUND', (c_idx, r_idx), (c_idx, r_idx), colors.pink))
            elif cell == "Zamknięte": style_cmds.append(('TEXTCOLOR', (c_idx, r_idx), (c_idx, r_idx), colors.gray))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_monthly_schedule_pdf(month: int, year: int, schedule_data: dict, store_settings: dict, sick_leaves: List[dict], all_employees: List[dict]) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
    elements = []
    
    styles = getSampleStyleSheet()
    styles['Normal'].fontName = FONT_NAME
    styles['Title'].fontName = FONT_NAME_BOLD
    
    pl_months = {1: "Styczeń", 2: "Luty", 3: "Marzec", 4: "Kwiecień", 5: "Maj", 6: "Czerwiec", 7: "Lipiec", 8: "Sierpień", 9: "Wrzesień", 10: "Październik", 11: "Listopad", 12: "Grudzień"}
    month_str = pl_months.get(month, date(year, month, 1).strftime("%B"))
    
    elements.append(Paragraph(f"Grafik - {month_str} {year}", styles['Title']))
    elements.append(Spacer(1, 15))
    
    sorted_employees = sorted(all_employees, key=lambda x: (x.get('last_name', ''), x.get('first_name', '')))
    header_row = ["Dzień"]
    emp_ids = []
    
    for emp in sorted_employees:
        fn = emp.get('first_name', '')
        ln = emp.get('last_name', '')
        initial = ln[0] + "." if ln else ""
        header_row.append(f"{fn} {initial}")
        emp_ids.append(str(emp.get('_id')))
        
    table_data = [header_row]
    _, last_day = calendar.monthrange(year, month)
    schedule_content = schedule_data.get('schedule', {})
    employee_totals = {eid: 0.0 for eid in emp_ids}
    
    for day_num in range(1, last_day + 1):
        current_date = date(year, month, day_num)
        date_str = current_date.strftime("%Y-%m-%d")
        row = [str(day_num)]
        
        is_closed_day = schedule_content.get(date_str, {}).get("is_closed", False)
        
        for emp_id in emp_ids:
            cell_text = "" 
            if is_on_sick_leave(emp_id, current_date, sick_leaves):
                cell_text = "L4"
            elif is_closed_day:
                cell_text = "" # Zostawiamy puste dla estetyki
            elif date_str in schedule_content:
                day_schedule = schedule_content[date_str]
                for shift_name, shift_data in day_schedule.items():
                    if not isinstance(shift_data, dict): continue
                    if any(str(e.get('id')) == emp_id for e in shift_data.get('employees', [])):
                        s_time = parse_time(shift_data.get('start_time'))
                        e_time = parse_time(shift_data.get('end_time'))
                        if s_time and e_time:
                            cell_text = f"{s_time.strftime('%H:%M')} - {e_time.strftime('%H:%M')}"
                            employee_totals[emp_id] += calculate_hours(s_time, e_time)
                        else:
                            cell_text = shift_name
                        break
            row.append(cell_text)
        table_data.append(row)
        
    summary_row = ["Suma godzin:"]
    for emp_id in emp_ids:
        summary_row.append(f"{employee_totals[emp_id]:.0f}h")
    table_data.append(summary_row)
    
    available_width = 515
    num_emps = len(emp_ids)
    col_width = available_width / num_emps if num_emps > 0 else 100
    if col_width < 50:
         doc.pagesize = landscape(A4)
         col_width = 802 / num_emps
    
    table = Table(table_data, colWidths=[40] + [col_width] * num_emps, repeatRows=1)
    
    style_cmds = [
        ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('FONTNAME', (0, 0), (-1, 0), FONT_NAME_BOLD),
        ('BACKGROUND', (0, 1), (0, -2), colors.whitesmoke),
        ('FONTNAME', (0, 1), (0, -2), FONT_NAME_BOLD),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
        ('FONTNAME', (0, -1), (-1, -1), FONT_NAME_BOLD),
    ]
    for r_idx, row in enumerate(table_data):
        for c_idx, cell in enumerate(row):
            if cell == "L4": style_cmds.append(('BACKGROUND', (c_idx, r_idx), (c_idx, r_idx), colors.pink))
            elif "Zamknięte" in str(cell): style_cmds.append(('TEXTCOLOR', (c_idx, r_idx), (c_idx, r_idx), colors.gray))
    
    table.setStyle(TableStyle(style_cmds))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer

def generate_hours_report_pdf(report_data: List[Dict[str, Any]], start_date: date, end_date: date) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    
    styles = getSampleStyleSheet()
    styles['Normal'].fontName = FONT_NAME
    styles['Heading2'].fontName = FONT_NAME_BOLD
    
    grouped_data = {}
    for entry in report_data:
        user_id = entry['user_id']
        if user_id not in grouped_data:
            grouped_data[user_id] = {
                "first_name": entry.get("first_name", ""),
                "last_name": entry.get("last_name", ""),
                "entries": [],
                "total_hours": 0.0
            }
        grouped_data[user_id]["entries"].append(entry)
        grouped_data[user_id]["total_hours"] += entry.get("hours", 0.0)

    for user_id, data in grouped_data.items():
        header_text = f"Raport Godzin: {data['first_name']} {data['last_name']}"
        elements.append(Paragraph(header_text, styles['Heading2']))
        
        period_text = f"Okres: {start_date} - {end_date}"
        elements.append(Paragraph(period_text, styles['Normal']))
        elements.append(Spacer(1, 10))
        
        table_data = [['Data', 'Od', 'Do', 'Godziny']]
        
        sorted_entries = sorted(data['entries'], key=lambda x: x['date'])
        
        for entry in sorted_entries:
            d = entry['date']
            if isinstance(d, datetime): d = d.date()
            
            start_t = entry['start_time']
            end_t = entry['end_time']
            
            if isinstance(start_t, (time, datetime)):
                start_t = start_t.strftime("%H:%M")
            if isinstance(end_t, (time, datetime)):
                end_t = end_t.strftime("%H:%M")
                
            row = [
                str(d),
                str(start_t),
                str(end_t),
                f"{entry['hours']:.2f}"
            ]
            table_data.append(row)
            
        table_data.append(['', '', 'SUMA:', f"{data['total_hours']:.2f}"])
        
        table = Table(table_data, colWidths=[100, 80, 80, 80])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), FONT_NAME_BOLD),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -2), 1, colors.black),
            ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black),
            ('FONTNAME', (0, -1), (-1, -1), FONT_NAME_BOLD),
            ('FONTNAME', (0, 1), (-1, -2), FONT_NAME), 
        ]))
        
        elements.append(table)
        elements.append(Spacer(1, 20))

    if not grouped_data:
        elements.append(Paragraph("Brak danych dla wybranego okresu.", styles['Normal']))

    doc.build(elements)
    buffer.seek(0)
    return buffer
