from __future__ import annotations

import functools
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from gspread.exceptions import WorksheetNotFound
from gspread.utils import rowcol_to_a1

from src.settings import settings


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MONTHS_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# columnas fijas según tu plantilla
CARD_COLS = {
    "no": "A",
    "personas": "B",
    "mesa": "C",
    "mesero": "D",
    "importe": "E",
    "propina": "F",
    "responsable": "G",
    "tarjeta": "H",
    "numero": "I",
}

CASH_COLS = {
    "no": "A",
    "personas": "B",
    "mesa": "C",
    "mesero": "D",
    "importe": "E",
    "propina": "F",
    "responsable": "G",
}

TIP_SIDE_COLS = {
    "no": "K",
    "propina": "L",
}


@dataclass
class RuntimeContext:
    spreadsheet: gspread.Spreadsheet
    day_ws: gspread.Worksheet
    log_ws: gspread.Worksheet
    config: dict


def local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


@functools.lru_cache(maxsize=1)
def _credentials() -> Credentials:
    raw = settings.google_service_account_json.strip()

    if raw.startswith("{"):
        info = json.loads(raw)
        if "private_key" in info:
            info["private_key"] = info["private_key"].replace("\\n", "\n")
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    return Credentials.from_service_account_file(raw, scopes=SCOPES)


@functools.lru_cache(maxsize=1)
def _gspread_client() -> gspread.Client:
    return gspread.authorize(_credentials())


def _drive_service():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


def month_name_es(ticket_date: date) -> str:
    return MONTHS_ES[ticket_date.month - 1]


def normalize_header(header: str) -> str:
    header = (header or "").strip().lower()
    header = re.sub(r"[^a-z0-9]+", "_", header)
    return header.strip("_")


def parse_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def parse_float(value, default: float = 0.0) -> float:
    try:
        s = str(value).strip().replace("$", "").replace(",", "")
        return float(s)
    except Exception:
        return default


def safe_cell(v):
    return "" if v is None else v


def format_money(v: float | None):
    if v is None:
        return ""
    return round(float(v), 2)


def find_month_spreadsheet_id(ticket_date: date) -> str:
    month_name = month_name_es(ticket_date)
    drive = _drive_service()

    query = (
        f"name = '{month_name}' "
        f"and mimeType = 'application/vnd.google-apps.spreadsheet' "
        f"and '{settings.gdrive_year_folder_id}' in parents "
        f"and trashed = false"
    )

    resp = drive.files().list(
        q=query,
        fields="files(id,name)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=10,
    ).execute()

    files = resp.get("files", [])
    if not files:
        raise FileNotFoundError(
            f"No encontré el archivo mensual '{month_name}' dentro de la carpeta del año."
        )

    return files[0]["id"]


def open_month_spreadsheet(ticket_date: date) -> gspread.Spreadsheet:
    client = _gspread_client()
    spreadsheet_id = find_month_spreadsheet_id(ticket_date)
    return client.open_by_key(spreadsheet_id)


def read_config(spreadsheet: gspread.Spreadsheet) -> dict:
    ws = spreadsheet.worksheet("CONFIG")
    rows = ws.get_all_values()

    config = {}
    for row in rows[1:]:
        if not row or len(row) < 2:
            continue
        key = (row[0] or "").strip()
        value = (row[1] or "").strip()
        if key:
            config[key] = value
    return config


def ensure_day_sheet(spreadsheet: gspread.Spreadsheet, ticket_date: date, config: dict) -> gspread.Worksheet:
    day_name = str(ticket_date.day)

    try:
        return spreadsheet.worksheet(day_name)
    except WorksheetNotFound:
        template_name = config.get("plantilla_sheet_name", "PLANTILLA")
        template_ws = spreadsheet.worksheet(template_name)
        return template_ws.duplicate(new_sheet_name=day_name)


def get_runtime(ticket_date: date) -> RuntimeContext:
    spreadsheet = open_month_spreadsheet(ticket_date)
    config = read_config(spreadsheet)
    day_ws = ensure_day_sheet(spreadsheet, ticket_date, config)
    log_ws = spreadsheet.worksheet(config.get("log_sheet_name", "LOG"))
    return RuntimeContext(
        spreadsheet=spreadsheet,
        day_ws=day_ws,
        log_ws=log_ws,
        config=config,
    )


def get_log_records(log_ws: gspread.Worksheet) -> list[dict]:
    values = log_ws.get_all_values()
    if not values:
        return []

    raw_headers = values[0]
    headers = [normalize_header(h) for h in raw_headers]

    records = []
    for idx, row in enumerate(values[1:], start=2):
        record = {}
        for i, header in enumerate(headers):
            record[header] = row[i] if i < len(row) else ""
        record["_row"] = idx
        records.append(record)
    return records


def append_log_record(log_ws: gspread.Worksheet, payload: dict) -> int:
    headers_raw = log_ws.row_values(1)
    headers = [normalize_header(h) for h in headers_raw]
    row = [payload.get(h, "") for h in headers]

    log_ws.append_row(row, value_input_option="USER_ENTERED")
    return len(log_ws.get_all_values())


def update_log_row(log_ws: gspread.Worksheet, row_number: int, updates: dict) -> None:
    headers_raw = log_ws.row_values(1)
    headers = [normalize_header(h) for h in headers_raw]
    header_to_col = {h: i + 1 for i, h in enumerate(headers)}

    batch = []
    for key, value in updates.items():
        if key not in header_to_col:
            continue
        col = header_to_col[key]
        a1 = rowcol_to_a1(row_number, col)
        batch.append({
            "range": a1,
            "values": [[value]],
        })

    if batch:
        log_ws.batch_update(batch, value_input_option="USER_ENTERED")


def next_free_row(ws: gspread.Worksheet, start_row: int, end_row: int, probe_col: str) -> int:
    """Lee todo el rango de una vez en lugar de celda por celda."""
    cell_range = f"{probe_col}{start_row}:{probe_col}{end_row}"
    values = ws.get(cell_range)
    for i, row_vals in enumerate(values):
        if not row_vals or not str(row_vals[0] if row_vals else "").strip():
            return start_row + i
    # Si todas las filas con valores están llenas, la siguiente libre
    # es start_row + len(values) (si hay menos filas que el rango)
    if len(values) < (end_row - start_row + 1):
        return start_row + len(values)
    raise RuntimeError(f"No hay filas libres en {ws.title} para el rango {start_row}:{end_row}")


def resolve_card_code_sheet(config: dict, network: str | None, card_type: str | None) -> str:
    if not network:
        return ""

    network = network.lower()
    card_type = (card_type or "").lower()

    if network == "visa" and card_type == "credito":
        return config.get("visa_credito_code", "CV")
    if network == "visa" and card_type == "debito":
        return config.get("visa_debito_code", "DV")
    if network == "mastercard" and card_type == "credito":
        return config.get("mastercard_credito_code", "CMC")
    if network == "mastercard" and card_type == "debito":
        return config.get("mastercard_debito_code", "DMC")
    if network == "amex":
        return config.get("amex_code", "AMEX")

    return network.upper()


def write_tip_side_table(day_ws: gspread.Worksheet, config: dict, tip_amount: float) -> int:
    start_row = parse_int(config.get("propina_tarjeta_start_row"), 8)
    end_row = parse_int(config.get("propina_tarjeta_end_row"), 22)

    row = next_free_row(day_ws, start_row, end_row, TIP_SIDE_COLS["propina"])
    logical_no = row - start_row + 1

    day_ws.update(
        f"{TIP_SIDE_COLS['no']}{row}:{TIP_SIDE_COLS['propina']}{row}",
        [[logical_no, format_money(tip_amount)]],
        value_input_option="USER_ENTERED",
    )
    return row


def write_tarjeta(day_ws: gspread.Worksheet, config: dict, payload: dict, responsable: str) -> tuple[int, str]:
    start_row = parse_int(config.get("tarjeta_start_row"), 8)
    end_row = parse_int(config.get("tarjeta_end_row"), 30)

    row = next_free_row(day_ws, start_row, end_row, CARD_COLS["importe"])
    logical_no = row - start_row + 1

    tip = payload.get("tip_in_card")
    card_label = payload.get("card_code_sheet") or payload.get("card_network") or ""

    values = [[
        logical_no,
        safe_cell(payload.get("personas")),
        safe_cell(payload.get("mesa")),
        safe_cell(payload.get("mesero")),
        format_money(payload.get("importe")),
        format_money(tip) if tip not in (None, "") else "",
        responsable,
        card_label,
        safe_cell(payload.get("card_last4")),
    ]]

    day_ws.update(
        f"{CARD_COLS['no']}{row}:{CARD_COLS['numero']}{row}",
        values,
        value_input_option="USER_ENTERED",
    )

    return row, config.get("ingreso_tarjeta_table_name", "ingreso_tarjeta")


def write_efectivo(day_ws: gspread.Worksheet, config: dict, payload: dict, responsable: str) -> tuple[int, str]:
    start_row = parse_int(config.get("efectivo_start_row"), 40)
    end_row = parse_int(config.get("efectivo_end_row"), 60)

    row = next_free_row(day_ws, start_row, end_row, CASH_COLS["importe"])
    logical_no = row - start_row + 1

    values = [[
        logical_no,
        safe_cell(payload.get("personas")),
        safe_cell(payload.get("mesa")),
        safe_cell(payload.get("mesero")),
        format_money(payload.get("importe")),
        format_money(payload.get("tip_in_cash")) if payload.get("tip_in_cash") not in (None, "") else "",
        responsable,
    ]]

    day_ws.update(
        f"{CASH_COLS['no']}{row}:{CASH_COLS['responsable']}{row}",
        values,
        value_input_option="USER_ENTERED",
    )

    return row, config.get("ingreso_efectivo_table_name", "ingreso_efectivo")


def write_propina_tarjeta_efectivo(
    day_ws: gspread.Worksheet,
    config: dict,
    pending_record: dict,
    tip_amount: float,
    tip_target_mode: str | None = None,
) -> None:
    target_table = str(pending_record["target_table"])
    target_row_raw = str(pending_record["target_row"])

    card_table = config.get("ingreso_tarjeta_table_name", "ingreso_tarjeta")
    cash_table = config.get("ingreso_efectivo_table_name", "ingreso_efectivo")

    if target_table == card_table:
        row = int(target_row_raw)
        if tip_target_mode == "card":
            day_ws.update(
                f"{CARD_COLS['propina']}{row}",
                [[format_money(tip_amount)]],
                value_input_option="USER_ENTERED",
            )
        elif tip_target_mode == "cash":
            # Escenario 2: Propina de tarjeta en efectivo -> tabla lateral
            write_tip_side_table(day_ws, config, tip_amount)
        else:
            # Fallback seguro
            day_ws.update(
                f"{CARD_COLS['propina']}{row}",
                [[format_money(tip_amount)]],
                value_input_option="USER_ENTERED",
            )

    elif target_table == cash_table:
        row = int(target_row_raw)
        day_ws.update(
            f"{CASH_COLS['propina']}{row}",
            [[format_money(tip_amount)]],
            value_input_option="USER_ENTERED",
        )

    elif target_table == "mixto":
        try:
            card_row_str, cash_row_str = target_row_raw.split("|")
            card_row = int(card_row_str)
            cash_row = int(cash_row_str)
        except Exception as e:
            raise RuntimeError(f"No pude leer target_row mixto={target_row_raw}") from e

        if tip_target_mode == "card":
            day_ws.update(
                f"{CARD_COLS['propina']}{card_row}",
                [[format_money(tip_amount)]],
                value_input_option="USER_ENTERED",
            )
        elif tip_target_mode == "cash":
            day_ws.update(
                f"{CASH_COLS['propina']}{cash_row}",
                [[format_money(tip_amount)]],
                value_input_option="USER_ENTERED",
            )
        else:
            raise RuntimeError("Para un ticket mixto necesito tip_target_mode='card' o 'cash'.")

    else:
        raise RuntimeError(f"No reconozco target_table={target_table}")


def build_log_payload(
    *,
    parsed: dict,
    responsable: str,
    target_table: str,
    target_row: int | str,
    telegram_chat_id: str,
    telegram_file_id: str,
    status: str,
    tip_in_card: float | None = None,
    tip_in_cash: float | None = None,
    tip_mode_final: str = "",
    record_id: str | None = None,
) -> dict:
    raw_ticket_date = parsed.get("ticket_date") or local_now().date().isoformat()
    ticket_dt = date.fromisoformat(raw_ticket_date)

    importe = parsed.get("importe") or 0.0
    total_cobrado = importe + (tip_in_card or 0.0) + (tip_in_cash or 0.0)

    return {
        "record_id": record_id or uuid.uuid4().hex[:12],
        "created_at": local_now().isoformat(),
        "ticket_date": raw_ticket_date,
        "year": ticket_dt.year,
        "month_name": month_name_es(ticket_dt),
        "day_sheet": str(ticket_dt.day),
        "payment_method": parsed.get("payment_method", ""),
        "mesa": parsed.get("mesa", ""),
        "mesero": parsed.get("mesero", ""),
        "personas": parsed.get("personas", ""),
        "importe": format_money(parsed.get("importe")),
        "tip_in_card": format_money(tip_in_card),
        "tip_in_cash": format_money(tip_in_cash),
        "tip_mode_final": tip_mode_final,
        "card_network": parsed.get("card_network", ""),
        "card_type": parsed.get("card_type", ""),
        "card_code_sheet": parsed.get("card_code_sheet", ""),
        "card_last4": parsed.get("card_last4", ""),
        "voucher_operation": parsed.get("voucher_operation", ""),
        "total_cobrado": format_money(total_cobrado),
        "responsable": responsable,
        "target_table": target_table,
        "target_row": target_row,
        "telegram_chat_id": str(telegram_chat_id),
        "telegram_file_id": telegram_file_id,
        "ocr_raw_text": parsed.get("ocr_raw_text", ""),
        "status": status,
    }

def is_duplicate(log_ws: gspread.Worksheet, config: dict, parsed: dict) -> bool:
    key_fields_raw = config.get("duplicate_key_fields", "ticket_date|mesa|importe|card_last4")
    key_fields = [normalize_header(x) for x in key_fields_raw.split("|") if x.strip()]
    records = get_log_records(log_ws)

    probe = {
        "ticket_date": str(parsed.get("ticket_date") or ""),
        "mesa": str(parsed.get("mesa") or ""),
        "importe": str(format_money(parsed.get("importe")) or ""),
        "card_last4": str(parsed.get("card_last4") or ""),
    }

    for rec in records:
        status = (rec.get("status") or "").upper()
        if status in {"OCR_FAILED", "PARSE_FAILED"}:
            continue

        matches = True
        for field in key_fields:
            left = str(rec.get(field, "") or "").strip()
            right = str(probe.get(field, "") or "").strip()
            if left != right:
                matches = False
                break

        if matches:
            return True

    return False


def find_latest_pending_for_chat(log_ws: gspread.Worksheet, chat_id: str | int) -> dict | None:
    chat_id = str(chat_id)
    records = get_log_records(log_ws)
    pending_statuses = {"PENDING_TIP_CARD", "PENDING_TIP_EFECTIVO", "PENDING_TIP_MIXTO"}

    for rec in reversed(records):
        if str(rec.get("telegram_chat_id", "")) != chat_id:
            continue
        if str(rec.get("status", "")).upper() in pending_statuses:
            return rec

    return None