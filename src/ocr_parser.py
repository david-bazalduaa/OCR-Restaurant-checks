from __future__ import annotations

import re
import unicodedata
from datetime import datetime, date
from io import BytesIO
from zoneinfo import ZoneInfo

import pytesseract
from PIL import Image, ImageOps, ImageFilter
from pytesseract import Output

from .settings import settings


CARD_WORDS = ["VISA", "MASTERCARD", "MASTER CARD", "AMEX", "AMERICAN EXPRESS"]


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(ch)
    )


def normalize_text(text: str) -> str:
    text = strip_accents(text or "")
    text = text.upper()
    text = text.replace("|", " ")
    text = text.replace("»", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def normalize_line(line: str) -> str:
    return normalize_text(line).replace("  ", " ").strip()


def parse_amount(raw: str | None) -> float | None:
    if raw is None:
        return None

    s = raw.strip()
    s = s.replace("$", "").replace("MXN", "").replace(" ", "")

    if "," in s and "." in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    else:
        if "," in s:
            # si parece decimal con coma, cámbiala
            if re.search(r",\d{2}$", s):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")

    try:
        return round(float(s), 2)
    except ValueError:
        return None


def money_candidates_from_line(line: str) -> list[float]:
    # exige al menos 1 separador decimal o miles para evitar confundir last4
    found = re.findall(r"\$?\s*\d[\d,.]*", line)
    values = []
    for item in found:
        value = parse_amount(item)
        if value is not None:
            values.append(value)
    return values


def preprocess_image(image_bytes: bytes, binary: bool = False) -> Image.Image:
    image = Image.open(BytesIO(image_bytes)).convert("L")
    image = ImageOps.autocontrast(image)
    image = image.filter(ImageFilter.SHARPEN)
    image = image.resize((image.width * 2, image.height * 2))
    if binary:
        image = image.point(lambda p: 255 if p > 170 else 0)
    return image


def _ocr_pass(image: Image.Image, psm: int) -> tuple[str, float | None]:
    config = f"--oem 3 --psm {psm}"
    raw_text = pytesseract.image_to_string(image, lang="spa+eng", config=config)

    data = pytesseract.image_to_data(
        image,
        lang="spa+eng",
        config=config,
        output_type=Output.DICT,
    )
    confs = []
    for c in data.get("conf", []):
        try:
            v = float(c)
            if v >= 0:
                confs.append(v)
        except Exception:
            pass

    confidence = round((sum(confs) / len(confs)) / 100, 3) if confs else None
    return raw_text, confidence


def merge_ocr_texts(texts: list[str]) -> str:
    seen = set()
    merged_lines = []

    for text in texts:
        for line in text.splitlines():
            cleaned = normalize_line(line)
            if not cleaned:
                continue
            key = re.sub(r"\s+", " ", cleaned)
            if key in seen:
                continue
            seen.add(key)
            merged_lines.append(line.strip())

    return "\n".join(merged_lines)


def run_ocr(image_bytes: bytes) -> tuple[str, float | None]:
    """2 pasadas: gris PSM6 + binario PSM6 (tickets rectangulares)."""
    gray = preprocess_image(image_bytes, binary=False)
    bw = preprocess_image(image_bytes, binary=True)

    texts = []
    confs = []

    for img in [gray, bw]:
        text, conf = _ocr_pass(img, 6)
        texts.append(text)
        if conf is not None:
            confs.append(conf)

    merged = merge_ocr_texts(texts)
    confidence = round(sum(confs) / len(confs), 3) if confs else None
    return merged, confidence


def extract_ticket_date(text: str) -> str | None:
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text)
    if not m:
        return None

    dd, mm, yy = m.groups()
    year = int(yy)
    if year < 100:
        year += 2000

    try:
        d = date(year, int(mm), int(dd))
        return d.isoformat()
    except ValueError:
        return None


def validate_mesa(raw: str | None) -> str | None:
    """Normaliza la mesa al formato letra+2dígitos (ej: A12, M04)."""
    if not raw:
        return None
    raw = raw.strip().upper()
    # Ya cumple el formato esperado
    if re.fullmatch(r"[A-Z]\d{2}", raw):
        return raw
    # Letra + 1 dígito → agregar cero (ej: M4 → M04)
    m = re.fullmatch(r"([A-Z])(\d)", raw)
    if m:
        return f"{m.group(1)}0{m.group(2)}"
    # Solo dígitos con 2+ chars → intentar interpretar como mesa numérica
    if re.fullmatch(r"\d{2,3}", raw):
        return raw
    # Devolver tal cual si no encaja
    return raw


def extract_mesa(text: str) -> str | None:
    # Prioridad 1: patrón explícito letra+2dígitos cerca de "MESA"
    m = re.search(r"\bMESA\s*[:#-]?\s*([A-Z]\d{2})\b", text)
    if m:
        return m.group(1).strip()
    # Prioridad 2: patrón general MESA + algo alfanumérico
    patterns = [
        r"\bMESA\s*[:#-]?\s*([A-Z0-9\-]{1,12})\b",
        r"\bMESA\s*#\s*([A-Z0-9\-]{1,12})\b",
        r"\bSECCION\s*[:#-]?\s*([A-Z0-9\-]{1,12})\b",
        r"\bTABLE\s*[:#-]?\s*([A-Z0-9\-]{1,12})\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return validate_mesa(m.group(1).strip())
    return None


def extract_personas(text: str) -> int | None:
    patterns = [
        r"#\s*PERS\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPERSONAS?\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPAX\s*[:#-]?\s*(\d{1,2})\b",
        r"\bCOMENSALES?\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPERS\s*[:#-]?\s*(\d{1,2})\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return int(m.group(1))
    return None


def extract_mesero(text: str) -> str | None:
    patterns = [
        r"\bMESERO\s*[:#-]?\s*([A-ZÑ ]{3,40})",
        r"\bATENDIO\s*[:#-]?\s*([A-ZÑ ]{3,40})",
        r"\bVENDEDOR\s*[:#-]?\s*([A-ZÑ ]{3,40})",
        r"\bCAJERO\s*[:#-]?\s*([A-ZÑ ]{3,40})",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            candidate = m.group(1).strip()
            candidate = re.sub(r"\s{2,}", " ", candidate)
            candidate = candidate.split("  ")[0].strip()
            if len(candidate) >= 3:
                return candidate.title()
    return None


def detect_card_network(text: str) -> str | None:
    if "MASTERCARD" in text or "MASTER CARD" in text:
        return "mastercard"
    if "AMEX" in text or "AMERICAN EXPRESS" in text:
        return "amex"
    if "VISA" in text:
        return "visa"
    return None


def detect_card_type(text: str) -> str | None:
    if "DEBITO" in text or "DEBIT" in text:
        return "debito"
    if "CREDITO" in text or "CREDIT" in text:
        return "credito"
    return None


def extract_last4(text: str) -> str | None:
    patterns = [
        r"(?:VISA|MASTERCARD|MASTER CARD|AMEX)[^\d]{0,10}(\d{4})\b",
        r"(?:->|TERMINACION|ULTIMOS?\s*4\s*DIGITOS|NUMERO)[^\d]{0,5}(\d{4})\b",
        r"(?:\*{2,}|X{2,})\s*(\d{4})\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None


def extract_voucher_operation(text: str) -> str | None:
    patterns = [
        r"\bOPERACION\s*#?\s*([A-Z0-9\-]{6,25})\b",
        r"\bAUTORIZACION\s*[:#-]?\s*([A-Z0-9\-]{4,25})\b",
        r"\bAUTH\s*[:#-]?\s*([A-Z0-9\-]{4,25})\b",
        r"\bREFERENCIA\s*[:#-]?\s*([A-Z0-9\-]{4,25})\b",
        r"\bAPROBACION\s*[:#-]?\s*([A-Z0-9\-]{4,25})\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None


def extract_first_amount_after(line: str, anchor_patterns: list[str]) -> float | None:
    for anchor in anchor_patterns:
        m = re.search(anchor + r"[^\d$]{0,10}\$?\s*([0-9][\d.,]*)", line)
        if m:
            return parse_amount(m.group(1))
    return None


def extract_payment_breakdown(lines: list[str]) -> dict:
    cash_amount = None
    card_amount = None
    network = None
    last4 = None

    for raw_line in lines:
        line = normalize_line(raw_line)
        if "PAGO" not in line:
            continue

        if "EFECTIVO" in line:
            value = extract_first_amount_after(line, [r"EFECTIVO"])
            if value is not None:
                cash_amount = value

        if "TARJETA" in line or any(word in line for word in CARD_WORDS):
            value = extract_first_amount_after(line, [r"TARJETA", r"VISA", r"MASTERCARD", r"MASTER CARD", r"AMEX"])
            if value is not None:
                card_amount = value

            line_network = detect_card_network(line)
            if line_network:
                network = line_network

            m_last4 = re.search(r"(?:->|/|-|\s)(\d{4})\b", line)
            if m_last4:
                last4 = m_last4.group(1)

    return {
        "cash_amount": cash_amount,
        "card_amount": card_amount,
        "card_network_from_payment": network,
        "card_last4_from_payment": last4,
    }


def find_amount_after_keywords(lines: list[str], keywords: list[str]) -> float | None:
    for line in lines:
        if any(k in line for k in keywords):
            values = money_candidates_from_line(line)
            if values:
                return values[-1]
    return None


def detect_payment_method(
    normalized_text: str,
    cash_amount: float | None,
    card_amount: float | None,
    card_network: str | None,
    last4: str | None,
) -> str:
    if cash_amount not in (None, 0) and card_amount not in (None, 0):
        return "mixto"

    if card_amount not in (None, 0):
        return "tarjeta"

    if cash_amount not in (None, 0):
        return "efectivo"

    if card_network or last4:
        return "tarjeta"

    if re.search(r"\bVISA\b|\bMASTERCARD\b|\bMASTER CARD\b|\bAMEX\b|\bDEBITO\b|\bCREDITO\b", normalized_text):
        return "tarjeta"

    if re.search(r"\bEFECTIVO\b|\bCASH\b", normalized_text):
        return "efectivo"

    return "desconocido"


def parse_ticket(raw_text: str) -> dict:
    raw_text = raw_text or ""
    normalized = normalize_text(raw_text)
    lines = [normalize_line(x) for x in raw_text.splitlines() if x.strip()]

    restaurant_total = find_amount_after_keywords(lines, ["TOTALES", "TOTAL CONSUMO", "TOTAL:", "TOTALES:"])
    voucher_total = find_amount_after_keywords(lines, ["TOTAL"])
    voucher_sale = find_amount_after_keywords(lines, ["VENTA"])
    propina = find_amount_after_keywords(lines, ["PROPINA", "TIP"])

    payment_breakdown = extract_payment_breakdown(lines)
    cash_amount = payment_breakdown["cash_amount"]
    card_amount = payment_breakdown["card_amount"]

    card_network = (
        payment_breakdown["card_network_from_payment"]
        or detect_card_network(normalized)
    )
    card_type = detect_card_type(normalized)
    card_last4 = (
        payment_breakdown["card_last4_from_payment"]
        or extract_last4(normalized)
    )

    payment_method = detect_payment_method(
        normalized,
        cash_amount,
        card_amount,
        card_network,
        card_last4,
    )

    if payment_method == "tarjeta":
        importe = voucher_sale or card_amount or restaurant_total or voucher_total
    elif payment_method == "efectivo":
        importe = cash_amount or restaurant_total or voucher_total
    elif payment_method == "mixto":
        importe = restaurant_total or voucher_total or ((cash_amount or 0) + (card_amount or 0))
    else:
        importe = restaurant_total or voucher_sale or voucher_total

    ticket_date = extract_ticket_date(normalized)
    mesa = extract_mesa(normalized)
    personas = extract_personas(normalized)
    mesero = extract_mesero(normalized)
    voucher_operation = extract_voucher_operation(normalized)

    return {
        "ticket_date": ticket_date,
        "mesa": mesa,
        "mesero": mesero,
        "personas": personas,
        "importe": importe,
        "propina": propina,
        "payment_method": payment_method,
        "cash_amount": cash_amount,
        "card_amount": card_amount,
        "card_network": card_network,
        "card_type": card_type,
        "card_last4": card_last4,
        "voucher_operation": voucher_operation,
        "voucher_sale": voucher_sale,
        "total_detected": voucher_total or restaurant_total,
        "ocr_raw_text": raw_text,
    }


def ocr_and_parse(image_bytes: bytes) -> dict:
    raw_text, confidence = run_ocr(image_bytes)
    parsed = parse_ticket(raw_text)
    parsed["ocr_confidence"] = confidence
    return parsed


def parse_tip_reply_message(text: str) -> dict | None:
    normalized = normalize_text(text)

    if normalized in {"0", "$0", "SIN PROPINA", "NO HUBO PROPINA"}:
        return {"amount": 0.0, "mode": None}

    mode = None
    if "TARJETA" in normalized or "CARD" in normalized:
        mode = "card"
    elif "EFECTIVO" in normalized or "CASH" in normalized:
        mode = "cash"

    candidates = re.findall(r"\$?\s*\d[\d.,]{0,15}", normalized)
    if not candidates:
        return None

    values = [parse_amount(x) for x in candidates]
    values = [x for x in values if x is not None]
    if not values:
        return None

    return {"amount": values[0], "mode": mode}


def local_today_iso() -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    return now.date().isoformat()