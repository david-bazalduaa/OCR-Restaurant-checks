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

def normalize_text_for_search(text: str) -> str:
    t = normalize_text(text)
    return re.sub(r"[^A-Z0-9$#%&]", "", t)

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
            if re.search(r",\d{2}$", s):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")

    try:
        return round(float(s), 2)
    except ValueError:
        return None

def parse_amount_strict(raw: str | None) -> float | None:
    """Enforces strictly 2 decimal places and handles OCR noise."""
    if not raw:
        return None
    s = raw.strip().replace("$", "").replace("MXN", "").replace(" ", "").replace("|", "").replace(">", "")
    m = re.search(r"([\d.,]+)[.,](\d{2})$", s)
    if m:
        integer_part = m.group(1)
        decimal_part = m.group(2)
        integer_part = re.sub(r"[.,]", "", integer_part)
        try:
            return round(float(f"{integer_part}.{decimal_part}"), 2)
        except ValueError:
            pass
    # fallback to old logic for "Siempre con dos decimales ... si está claro"
    val = parse_amount(raw)
    return val

def preprocess_image(image_bytes: bytes, binary: bool = False) -> Image.Image:
    image = Image.open(BytesIO(image_bytes)).convert("L")
    image = ImageOps.autocontrast(image)
    image = image.filter(ImageFilter.SHARPEN)
    image = image.resize((image.width * 2, image.height * 2))
    if binary:
        image = image.point(lambda p: 255 if p > 170 else 0)
    return image

# -------- SPATIAL PARSER --------

def extract_spatial_data(image: Image.Image, psm: int) -> list[dict]:
    config = f"--oem 3 --psm {psm}"
    data = pytesseract.image_to_data(
        image,
        lang="spa+eng",
        config=config,
        output_type=Output.DICT,
    )
    words = []
    text_len = len(data.get("text", []))
    for i in range(text_len):
        text = data["text"][i].strip()
        try:
            conf_val = float(data["conf"][i])
        except Exception:
            conf_val = -1.0
        
        if text and conf_val >= 0:
            words.append({
                "text": text,
                "norm": normalize_text(text),
                "search": normalize_text_for_search(text),
                "left": data["left"][i],
                "top": data["top"][i],
                "width": data["width"][i],
                "height": data["height"][i],
                "conf": conf_val,
                "right": data["left"][i] + data["width"][i],
                "bottom": data["top"][i] + data["height"][i],
            })
    return words

def group_words_into_lines(words: list[dict], y_tolerance: int = 20) -> list[list[dict]]:
    if not words:
        return []
    words = sorted(words, key=lambda w: w["top"])
    lines = []
    current_line = []
    current_top = None
    
    for w in words:
        if current_top is None:
            current_top = w["top"]
            current_line.append(w)
        elif abs(w["top"] - current_top) <= y_tolerance:
            current_line.append(w)
            current_top = sum(x["top"] for x in current_line) / len(current_line)
        else:
            lines.append(sorted(current_line, key=lambda x: x["left"]))
            current_line = [w]
            current_top = w["top"]
            
    if current_line:
        lines.append(sorted(current_line, key=lambda x: x["left"]))
        
    lines = sorted(lines, key=lambda l: l[0]["top"])
    return lines

def run_ocr_spatial(image_bytes: bytes) -> tuple[str, list[list[dict]], float | None]:
    # Hacemos el pass principal gris
    gray = preprocess_image(image_bytes, binary=False)
    words = extract_spatial_data(gray, 6)
    
    confs = [w["conf"] for w in words if w["conf"] >= 0]
    confidence = round((sum(confs) / len(confs)) / 100, 3) if confs else None
    
    lines = group_words_into_lines(words, y_tolerance=20)
    
    raw_text_lines = []
    for line in lines:
        raw_text_lines.append(" ".join([w["text"] for w in line]))
    raw_text = "\n".join(raw_text_lines)
    
    return raw_text, lines, confidence

# -------- EXTRACTORES --------

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
    """Normaliza la mesa al formato letra+2dígitos (ej: A12, M04) si tiene sentido."""
    if not raw:
        return None
    raw = raw.strip().upper()
    raw = re.sub(r"^[^A-Z0-9]+", "", raw)
    raw = re.sub(r"[^A-Z0-9]+$", "", raw)
    
    # REGLA: Si lee un 1 al inicio, convertir a "I" mayuscula.
    if raw.startswith("1") and len(raw) >= 2:
        raw = "I" + raw[1:]
    
    if re.fullmatch(r"[A-Z]\d{2}", raw):
        return raw
    m = re.fullmatch(r"([A-Z])(\d)", raw)
    if m:
        return f"{m.group(1)}0{m.group(2)}"
    if re.fullmatch(r"\d{1,3}", raw):
        return raw
    m = re.search(r"\b([A-Z]\d{1,2})\b", raw)
    if m:
         return validate_mesa(m.group(1))
    
    if len(raw) <= 5 and re.match(r"^[A-Z0-9]*\d+[A-Z0-9]*$", raw):
         return raw
    return None

def get_line_text_from(line: list[dict], start_idx: int) -> str:
    return " ".join([w["text"] for w in line[start_idx:]])

def extract_mesa_spatial(lines: list[list[dict]]) -> str | None:
    # Prioridad espacial
    for i, line in enumerate(lines):
        for j, w in enumerate(line):
            t = w["search"]
            if t in ["MESA", "NESA", "SECCION", "TABLE"] or (t.startswith("MESA") and len(t) <= 6):
                # Caso 1: misma linea a la derecha
                if j + 1 < len(line):
                    candidate_str = get_line_text_from(line, j + 1)
                    val = validate_mesa(candidate_str)
                    if val: return val
                    val = validate_mesa(line[j+1]["text"])
                    if val: return val
                # Caso 2: linea debajo
                if i + 1 < len(lines):
                    candidate_str = get_line_text_from(lines[i+1], 0)
                    val = validate_mesa(candidate_str)
                    if val: return val
    
    # Prioridad regex sobre text plano si falló lo espacial
    text = "\n".join([get_line_text_from(l, 0) for l in lines])
    m = re.search(r"\bMESA\s*[:#-]?\s*([A-Z0-9\-]{1,12})\b", text, re.I)
    if m: 
        return validate_mesa(m.group(1).strip())
    m = re.search(r"\bMESA\s*#\s*([A-Z0-9\-]{1,12})\b", text, re.I)
    if m:
        return validate_mesa(m.group(1).strip())
    
    return None

def extract_personas_spatial(lines: list[list[dict]]) -> int | None:
    # Prioridad espacial
    for i, line in enumerate(lines):
        line_text = " ".join([w["norm"] for w in line])
        for j, w in enumerate(line):
            t = w["search"]
            if ("PERS" in t or "PAX" in t or "COMENSAL" in t or "PARS" in t or t.endswith("PER")) and "PERSONA" not in t:
                # buscar numero a la derecha
                for k in range(j+1, len(line)):
                    num_str = re.sub(r"[^\d]", "", line[k]["search"])
                    if num_str:
                        return int(num_str)
                # buscar numero abajo
                if i + 1 < len(lines):
                    for xw in lines[i+1]:
                        if xw["left"] >= w["left"] - 50:
                            num_str = re.sub(r"[^\d]", "", xw["search"])
                            if num_str: return int(num_str)
    
    # Fallback si OCR unió el texto "#PERS 3" > "#PERS3"
    for line in lines:
        for w in line:
            m = re.search(r"(?:#?PERS|PAX|COMENSALES?)[^\d]*(\d{1,2})", w["search"])
            if m:
                return int(m.group(1))
                
    # Fallback clásico
    text = "\n".join([get_line_text_from(l, 0) for l in lines])
    patterns = [
        r"#\s*PERS\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPERSONAS?\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPAX\s*[:#-]?\s*(\d{1,2})\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
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
    if "MASTERCARD" in text or "MASTER CARD" in text: return "mastercard"
    if "AMEX" in text or "AMERICAN EXPRESS" in text: return "amex"
    if "VISA" in text: return "visa"
    return None

def detect_card_type(text: str) -> str | None:
    if "DEBITO" in text or "DEBIT" in text: return "debito"
    if "CREDITO" in text or "CREDIT" in text: return "credito"
    return None

def extract_last4(text: str) -> str | None:
    patterns = [
        r"(?:VISA|MASTERCARD|MASTER CARD|AMEX)[^\d]{0,10}(\d{4})\b",
        r"(?:->|TERMINACION|ULTIMOS?\s*4\s*DIGITOS|NUMERO)[^\d]{0,5}(\d{4})\b",
        r"(?:\*{2,}|X{2,})\s*(\d{4})\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m: return m.group(1)
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
        m = re.search(p, text, re.I)
        if m: return m.group(1)
    return None

def get_ticket_width(lines: list[list[dict]]) -> int:
    max_right = 0
    for l in lines:
        for w in l:
            if w["right"] > max_right:
                max_right = w["right"]
    return max_right if max_right > 0 else 1000

def extract_amounts_spatial(lines: list[list[dict]], page_width: int):
    # Regla: separar la columna derecha (> 60% del ticket ancho aprox)
    right_margin = page_width * 0.60
    
    main_amounts = []
    right_amounts = []
    propina = None
    
    for i, l in enumerate(lines):
        line_text = " ".join([w["norm"] for w in l])
        is_propina_label = "PROPINA" in line_text or "TIP" in line_text
        
        for w in l:
            amt = parse_amount_strict(w["text"])
            if amt is not None and amt > 0:
                if w["left"] >= right_margin:
                    right_amounts.append({"amount": amt, "word": w, "line_text": line_text, "line_idx": i})
                    if is_propina_label:
                        propina = amt
                else:
                    main_amounts.append({"amount": amt, "word": w, "line_text": line_text, "line_idx": i})
                    
    # Si la propina explícita en la columna derecha no salió, busquémosla asociándola a "PROPINA" en toda la linea
    if propina is None:
        for i, l in enumerate(lines):
            line_text = " ".join([w["norm"] for w in l])
            if "PROPINA" in line_text or "TIP" in line_text:
                for w in l:
                    if "PROPINA" in w["search"] or "TIP" in w["search"]:
                        # derecha en misma linea
                        for w2 in l:
                            if w2["left"] > w["left"]:
                                amt = parse_amount_strict(w2["text"])
                                if amt is not None:
                                    propina = amt
                                    break
                        if propina: break
                        # abajo en siguiente linea
                        if i + 1 < len(lines):
                            for w2 in lines[i+1]:
                                amt = parse_amount_strict(w2["text"])
                                if amt is not None and w2["left"] >= w["left"] - 80:
                                    propina = amt
                                    break
                if propina: break

    # Evitamos que asigne el total del ticket si por alguna razon OCR lo vio como propina por proximidad
    return main_amounts, right_amounts, propina

def find_amount_by_keyword_spatial(amounts: list[dict], keywords: list[str]) -> float | None:
    for item in amounts:
        for k in keywords:
            if k in item["line_text"]:
                return item["amount"]
    return None

def extract_payment_breakdown_spatial(lines: list[list[dict]]) -> dict:
    cash_amount = None
    card_amount = None
    network = None
    last4 = None

    for l in lines:
        line_text = " ".join([w["norm"] for w in l])
        if "PAGO" not in line_text: continue
        
        amounts_in_line = []
        for w in l:
            amt = parse_amount_strict(w["text"])
            if amt is not None and amt > 0: amounts_in_line.append(amt)
            
        if "EFECTIVO" in line_text and amounts_in_line:
            cash_amount = amounts_in_line[-1]
            
        if ("TARJETA" in line_text or any(cw in line_text for cw in CARD_WORDS)) and amounts_in_line:
            card_amount = amounts_in_line[-1]
            network = detect_card_network(line_text)
            m_last4 = re.search(r"(?:->|/|-|\s)(\d{4})\b", " ".join([w["text"] for w in l]))
            if m_last4:
                last4 = m_last4.group(1)
                
    return {
        "cash_amount": cash_amount,
        "card_amount": card_amount,
        "card_network_from_payment": network,
        "card_last4_from_payment": last4,
    }

def detect_payment_method(
    normalized_text: str,
    cash_amount: float | None,
    card_amount: float | None,
    card_network: str | None,
    last4: str | None,
) -> str:
    if cash_amount not in (None, 0) and card_amount not in (None, 0): return "mixto"
    if card_amount not in (None, 0): return "tarjeta"
    if cash_amount not in (None, 0): return "efectivo"
    if card_network or last4: return "tarjeta"
    if re.search(r"\bVISA\b|\bMASTERCARD\b|\bMASTER CARD\b|\bAMEX\b|\bDEBITO\b|\bCREDITO\b", normalized_text): return "tarjeta"
    if re.search(r"\bEFECTIVO\b|\bCASH\b", normalized_text): return "efectivo"
    return "desconocido"

def parse_ticket_spatial(raw_text: str, lines: list[list[dict]]) -> dict:
    normalized = normalize_text(raw_text)
    
    page_width = get_ticket_width(lines)
    # Extraemos montones principales (lado izquierdo/centro) y opcionalmente propinas (lado derecho)
    main_amounts, right_amounts, propina = extract_amounts_spatial(lines, page_width)
    
    # IMPORTANTE: Total solo debe salir del main ticket, excluyendo la derecha
    restaurant_total = find_amount_by_keyword_spatial(main_amounts, ["TOTALES", "TOTAL CONSUMO", "TOTAL:", "TOTALES:"])
    voucher_total = find_amount_by_keyword_spatial(main_amounts, ["TOTAL", "OTAL "])
    voucher_sale = find_amount_by_keyword_spatial(main_amounts, ["VENTA"])
    
    # Fallback si por alguna razon extrema nada caia en el "main":
    if not (restaurant_total or voucher_total):
        all_amts = main_amounts + right_amounts
        restaurant_total = find_amount_by_keyword_spatial(all_amts, ["TOTALES", "TOTAL CONSUMO", "TOTAL:", "TOTALES:"])
        voucher_total = find_amount_by_keyword_spatial(all_amts, ["TOTAL", "OTAL "])
        if not voucher_sale:
            voucher_sale = find_amount_by_keyword_spatial(all_amts, ["VENTA"])

    payment_breakdown = extract_payment_breakdown_spatial(lines)
    cash_amount = payment_breakdown["cash_amount"]
    card_amount = payment_breakdown["card_amount"]

    card_network = (payment_breakdown["card_network_from_payment"] or detect_card_network(normalized))
    card_type = detect_card_type(normalized)
    card_last4 = (payment_breakdown["card_last4_from_payment"] or extract_last4(normalized))

    payment_method = detect_payment_method(normalized, cash_amount, card_amount, card_network, card_last4)

    if payment_method == "tarjeta":
        importe = voucher_sale or card_amount or restaurant_total or voucher_total
    elif payment_method == "efectivo":
        importe = cash_amount or restaurant_total or voucher_total
    elif payment_method == "mixto":
        importe = restaurant_total or voucher_total or ((cash_amount or 0) + (card_amount or 0))
    else:
        importe = restaurant_total or voucher_sale or voucher_total

    ticket_date = extract_ticket_date(normalized)
    mesa = extract_mesa_spatial(lines)
    personas = extract_personas_spatial(lines)
    mesero = extract_mesero(normalized)
    voucher_operation = extract_voucher_operation(normalized)

    # Validacion extra de Propina cruzada
    # Para asegurar que no confunda el subtotal de la derecha con la propina ni propina con total
    # Si la "propina" es el exacto mismo valor que el total, significa que OCR confundio la columna del total o que no hay propina impresa distinta al total.
    if propina is not None and propina == importe:
        propina = None

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
    raw_text, lines, confidence = run_ocr_spatial(image_bytes)
    parsed = parse_ticket_spatial(raw_text, lines)
    parsed["ocr_confidence"] = confidence
    return parsed

def parse_tip_reply_message(text: str) -> dict | None:
    normalized = normalize_text(text)
    if normalized in {"0", "$0", "SIN PROPINA", "NO HUBO PROPINA"}:
        return {"amount": 0.0, "mode": None}

    mode = None
    if "TARJETA" in normalized or "CARD" in normalized: mode = "card"
    elif "EFECTIVO" in normalized or "CASH" in normalized: mode = "cash"

    candidates = re.findall(r"\$?\s*\d[\d.,]{0,15}", normalized)
    if not candidates: return None

    values = [parse_amount(x) for x in candidates]
    values = [x for x in values if x is not None]
    if not values: return None
    return {"amount": values[0], "mode": mode}

def local_today_iso() -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    return now.date().isoformat()