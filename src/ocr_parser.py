from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
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

def _merge_word_sets(primary: list[dict], secondary: list[dict], tol: int = 30) -> list[dict]:
    """Merge secondary words into primary when they don't overlap existing ones."""
    merged = list(primary)
    for sw in secondary:
        overlaps = False
        for pw in merged:
            if (abs(sw["top"] - pw["top"]) < tol and
                abs(sw["left"] - pw["left"]) < tol):
                overlaps = True
                break
        if not overlaps:
            merged.append(sw)
    return merged

def run_ocr_spatial(image_bytes: bytes) -> tuple[str, list[list[dict]], float | None]:
    # Pass 1: grayscale
    gray = preprocess_image(image_bytes, binary=False)
    words_gray = extract_spatial_data(gray, 6)
    
    # Pass 2: binary — rescues text from noisy/dark backgrounds
    bw = preprocess_image(image_bytes, binary=True)
    words_bw = extract_spatial_data(bw, 6)
    
    # Merge: keep gray as primary, add binary-only words
    words = _merge_word_sets(words_gray, words_bw)
    
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

# OCR letter corrections for mesa: common OCR misreads to valid IMSP letters
_MESA_LETTER_MAP = {
    "1": "I", "T": "I", "L": "I", "|": "I",  # visually similar to I
    "N": "M", "H": "M",                          # visually similar to M
    "5": "S",                                      # visually similar to S
    "D": "P", "B": "P",                            # visually similar to P
}

def validate_mesa(raw: str | None) -> str | None:
    """Normaliza la mesa al formato [IMSP]\d{2}. SOLO acepta letras I, M, S, P."""
    if not raw:
        return None
    raw = raw.strip().upper()
    
    # REGLA: Si lee un 1 al inicio, convertir a "I" mayuscula.
    if raw.startswith("1") and len(raw) >= 2:
        raw = "I" + raw[1:]
        
    raw = re.sub(r"[^A-Z0-9]+", "", raw)
    
    # Strict match: [IMSP] + 2 digits
    m = re.search(r"([IMSP])(\d{2})", raw)
    if m:
        return f"{m.group(1)}{m.group(2)}"
        
    # Tolerancia: 1 digito ej M4 -> M04
    m = re.search(r"([IMSP])(\d{1})(?!\d)", raw)
    if m:
        return f"{m.group(1)}0{m.group(2)}"
    
    # OCR correction: if letter is NOT [IMSP] but has a known correction, try it
    m = re.search(r"([A-Z])(\d{1,2})", raw)
    if m:
        letter = m.group(1)
        digits = m.group(2)
        corrected = _MESA_LETTER_MAP.get(letter)
        if corrected:
            digits = digits.zfill(2)
            return f"{corrected}{digits}"
    
    # DO NOT accept invalid letters (A, B, X, etc.) — return None
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
    _PERS_VARIANTS = {"PERS", "PCRS", "PER5", "PARS", "PERE", "PERZ"}
    
    def _is_pers_trigger(search_txt: str) -> bool:
        if "PERSONA" in search_txt:  # skip "x Persona: 166.25" 
            return False
        for v in _PERS_VARIANTS:
            if v in search_txt:
                return True
        if search_txt.endswith("PER") or "PAX" in search_txt or "COMENSAL" in search_txt:
            return True
        return False

    def _extract_int(s: str) -> int | None:
        d = re.sub(r"[^\d]", "", s)
        if d and 1 <= int(d) <= 99:
            return int(d)
        return None

    # --- Pass 1: Spatial search near # Pers label ---
    for i, line in enumerate(lines):
        for j, w in enumerate(line):
            t = w["search"]
            
            # Case A: word itself contains PERS variant
            if _is_pers_trigger(t):
                # embedded digit in same token e.g. "#PERS9" or "PERS:4"
                m = re.search(r"(?:" + "|".join(_PERS_VARIANTS) + r")[^\d]*(\d{1,2})", t)
                if m:
                    return int(m.group(1))
                # search right on same line
                for k in range(j + 1, len(line)):
                    val = _extract_int(line[k]["search"])
                    if val is not None:
                        return val
                # search line below, spatially aligned
                if i + 1 < len(lines):
                    for xw in lines[i + 1]:
                        if xw["left"] >= w["left"] - 80 and xw["left"] <= w["right"] + 80:
                            val = _extract_int(xw["search"])
                            if val is not None:
                                return val
            
            # Case B: "#" is a separate word, followed by "Pers" or variant
            if t == "#" and j + 1 < len(line):
                next_t = line[j + 1]["search"]
                if _is_pers_trigger(next_t) or any(v in next_t for v in _PERS_VARIANTS):
                    # embedded
                    m = re.search(r"(\d{1,2})", next_t)
                    if m:
                        return int(m.group(1))
                    # search further right
                    for k in range(j + 2, len(line)):
                        val = _extract_int(line[k]["search"])
                        if val is not None:
                            return val
                    # below
                    if i + 1 < len(lines):
                        for xw in lines[i + 1]:
                            val = _extract_int(xw["search"])
                            if val is not None:
                                return val

    # --- Pass 2: full-text regex fallback ---
    full_text = "\n".join([get_line_text_from(l, 0) for l in lines])
    patterns = [
        r"#\s*Pers\s*[:#-]?\s*(\d{1,2})\b",
        r"#\s*Pcrs\s*[:#-]?\s*(\d{1,2})\b",
        r"#\s*Per5\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPers\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPersonas?\s*[:#-]?\s*(\d{1,2})\b",
        r"\bPAX\s*[:#-]?\s*(\d{1,2})\b",
        r"\bComandas\s*[:#-]?\s*(\d{1,2})\b",
    ]
    for p in patterns:
        m = re.search(p, full_text, re.I)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 99:
                return val

    # --- Pass 3: count BUFFET ADULTO items as heuristic ---
    adulto_count = 0
    for line in lines:
        line_raw = " ".join([w["text"] for w in line])
        if re.search(r"BUFFET\s+ADULTO", line_raw, re.I):
            adulto_count += 1
    if adulto_count > 0:
        return adulto_count

    return None

def extract_mesero(text: str) -> str | None:
    # Broader regex: allow digits and special chars that OCR may inject
    patterns = [
        r"\bMESERO\s*[:#-]?\s*([A-ZÑ0-9 .]{2,40})",
        r"\bATENDIO\s*[:#-]?\s*([A-ZÑ0-9 .]{2,40})",
        r"\bVENDEDOR\s*[:#-]?\s*([A-ZÑ0-9 .]{2,40})",
        r"\bCAJERO\s*[:#-]?\s*([A-ZÑ0-9 .]{2,40})",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            candidate = m.group(1).strip()
            candidate = re.sub(r"\s{2,}", " ", candidate)
            # Take first word-chunk (before double-space or next label)
            candidate = re.split(r"\s{2,}|\bHORA\b|\bCAJERO\b|\bFECHA\b", candidate)[0].strip()
            # Strip trailing dots/digits that are pure noise
            candidate = re.sub(r"[.0-9]+$", "", candidate).strip()
            if len(candidate) >= 2:
                return candidate.title()
    return None


def _lcs_length(a: str, b: str) -> int:
    """Longest Common Subsequence length (not substring)."""
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return 0
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[n]


def _char_overlap_score(cand: str, name: str) -> float:
    """Fraction of name's unique chars that are present in candidate."""
    if not name:
        return 0.0
    name_chars = set(name)
    cand_chars = set(cand)
    if not name_chars:
        return 0.0
    return len(name_chars & cand_chars) / len(name_chars)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein edit distance."""
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


def resolve_mesero_flexible(candidate: str | None, config: dict) -> tuple[str | None, str | None]:
    """
    Resolve OCR mesero candidate to a canonical name from CONFIG.
    Returns (resolved_name, warning_or_None).
    
    ALWAYS picks the best candidate — never leaves mesero empty
    unless candidate text has zero alpha characters.
    Uses multi-metric scoring: aliases, containment, LCS, char overlap,
    edit distance, SequenceMatcher.
    """
    if not candidate:
        return None, None
    
    valid_waiters_str = config.get("valid_waiters", "")
    if not valid_waiters_str:
        return candidate, None  # no validation list configured
    
    official_names = [n.strip() for n in valid_waiters_str.split("|") if n.strip()]
    if not official_names:
        return candidate, None
    
    candidate_clean = re.sub(r"[^A-Z]", "", candidate.upper())
    if not candidate_clean:
        return candidate, "mesero_no_alpha"
    
    # --- Phase 1: Exact alias match (instant) ---
    for name in official_names:
        alias_key = f"waiter_aliases_{name.lower()}"
        aliases_str = config.get(alias_key, "")
        aliases = [a.strip().upper() for a in aliases_str.split("|") if a.strip()]
        if candidate.upper() in aliases or candidate_clean in aliases:
            return name, None
    
    # --- Phase 2: Multi-metric scoring (ALWAYS picks best) ---
    scores = []  # list of (combined_score, name)
    
    for name in official_names:
        name_clean = re.sub(r"[^A-Z]", "", name.upper())
        if not name_clean:
            continue
        
        max_len = max(len(candidate_clean), len(name_clean))
        
        # Metric 1: SequenceMatcher ratio (0-1)
        sm_ratio = SequenceMatcher(None, candidate_clean, name_clean).ratio()
        
        # Metric 2: LCS ratio — longest common subsequence / max length (0-1)
        lcs_len = _lcs_length(candidate_clean, name_clean)
        lcs_ratio = lcs_len / max_len if max_len > 0 else 0.0
        
        # Metric 3: Character overlap — fraction of name chars present in candidate (0-1)
        char_overlap = _char_overlap_score(candidate_clean, name_clean)
        
        # Metric 4: Containment bonus (0 or 0.3)
        containment = 0.3 if (name_clean in candidate_clean or candidate_clean in name_clean) else 0.0
        
        # Metric 5: Edit distance penalty (normalized 0-1, inverted)
        ed = _edit_distance(candidate_clean, name_clean)
        ed_score = 1.0 - (ed / max_len) if max_len > 0 else 0.0
        
        # Weighted combination
        combined = (
            sm_ratio * 0.25 +
            lcs_ratio * 0.25 +
            char_overlap * 0.20 +
            containment * 0.15 +
            ed_score * 0.15
        )
        
        scores.append((combined, name, sm_ratio))
    
    if not scores:
        return candidate, "mesero_no_match"
    
    # Sort by combined score descending — ALWAYS pick the best
    scores.sort(key=lambda x: x[0], reverse=True)
    best_combined, best_name, best_sm = scores[0]
    
    # Confidence-based warning (informational only — never blocks)
    if best_combined >= 0.55:
        warning = None  # high confidence
    elif best_combined >= 0.35:
        warning = f"mesero_inferido({best_combined:.0%})"
    else:
        warning = f"mesero_baja_evidencia({best_combined:.0%})"
    
    return best_name, warning

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

# Max plausible amount for a restaurant ticket (generous cap)
_MAX_PLAUSIBLE_AMOUNT = 50_000.00
_MIN_PLAUSIBLE_PROPINA = 5.00  # propina must be at least $5 to be credible

def _is_plausible_amount(amt: float | None) -> bool:
    """Check if an amount is plausible for a restaurant ticket."""
    if amt is None:
        return False
    return 0.01 <= amt <= _MAX_PLAUSIBLE_AMOUNT

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
            if amt is not None and _is_plausible_amount(amt):
                if w["left"] >= right_margin:
                    right_amounts.append({"amount": amt, "word": w, "line_text": line_text, "line_idx": i})
                    if is_propina_label and amt >= _MIN_PLAUSIBLE_PROPINA:
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
                                if amt is not None and amt >= _MIN_PLAUSIBLE_PROPINA:
                                    propina = amt
                                    break
                        if propina: break
                        # abajo en siguiente linea
                        if i + 1 < len(lines):
                            for w2 in lines[i+1]:
                                amt = parse_amount_strict(w2["text"])
                                if amt is not None and amt >= _MIN_PLAUSIBLE_PROPINA and w2["left"] >= w["left"] - 80:
                                    propina = amt
                                    break
                if propina: break

    # Fallback: text-based regex over full raw text
    if propina is None:
        full_raw = "\n".join([" ".join([w["text"] for w in l]) for l in lines])
        # Pattern: "Propina" followed by optional $ and number
        m = re.search(r"[Pp]ropina\s*[\$:]?\s*(\d[\d.,]*\.\d{2})", full_raw)
        if m:
            amt = parse_amount_strict(m.group(1))
            if amt is not None and amt >= _MIN_PLAUSIBLE_PROPINA:
                propina = amt

    # Fallback: if right_amounts has ≥3 items (Venta, Propina, Total layout), 
    # pick the middle value only if it's labeled or plausible as tip
    if propina is None and len(right_amounts) >= 3:
        sorted_right = sorted(right_amounts, key=lambda x: x["amount"])
        # The middle value is often the propina in a Venta/Propina/Total layout
        for item in sorted_right[:-1]:  # exclude the largest (Total)
            if item["amount"] >= _MIN_PLAUSIBLE_PROPINA and "PROPINA" in item.get("line_text", ""):
                propina = item["amount"]
                break

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

def _fallback_largest_amount(lines: list[list[dict]]) -> float | None:
    """Last resort: find the single largest PLAUSIBLE monetary amount on the ticket."""
    biggest = None
    for l in lines:
        line_text = " ".join([w["norm"] for w in l])
        # Skip lines that look like phone numbers, folios, references, dates
        if re.search(r"NUMERO|FOLIO|TELEFONO|TEL|SERIE|OPERACION|AUTORIZACION", line_text):
            continue
        for w in l:
            amt = parse_amount_strict(w["text"])
            if amt is not None and _is_plausible_amount(amt):
                if biggest is None or amt > biggest:
                    biggest = amt
    return biggest


def parse_ticket_spatial(raw_text: str, lines: list[list[dict]]) -> dict:
    normalized = normalize_text(raw_text)
    warnings = {}  # per-field warnings
    
    page_width = get_ticket_width(lines)
    main_amounts, right_amounts, propina = extract_amounts_spatial(lines, page_width)
    
    # ---- IMPORTE HIERARCHY ----
    # The key distinction:
    #   restaurant_total = TOTALES from main ticket (=consumo, THE correct importe)
    #   voucher_sale     = "Venta" from voucher   (=consumo base, good fallback)
    #   voucher_total    = "Total" from voucher   (=consumo+propina, NEVER use as importe)
    #
    # Priority: restaurant_total > voucher_sale > fallback
    # NEVER: voucher_total, card_amount (partial), cash_amount (partial)

    # Step 1: Find TOTALES from the main ticket (strict label match)
    restaurant_total = find_amount_by_keyword_spatial(
        main_amounts, ["TOTALES", "TOTAL CONSUMO", "TOTALES:"]
    )
    
    # Step 2: Find Venta from voucher (consumo base)
    voucher_sale = find_amount_by_keyword_spatial(main_amounts, ["VENTA"])
    if not voucher_sale:
        voucher_sale = find_amount_by_keyword_spatial(right_amounts, ["VENTA"])
    
    # Step 3: Find voucher Total (ONLY for metadata, never for importe)
    voucher_total = find_amount_by_keyword_spatial(right_amounts, ["TOTAL"])
    if not voucher_total:
        # Only match "TOTAL" in main if it's NOT the same as TOTALES
        for item in main_amounts:
            if "TOTAL" in item["line_text"] and "TOTALES" not in item["line_text"]:
                voucher_total = item["amount"]
                break
    
    # Step 4: Fallback — search across all amounts if main had nothing
    if not restaurant_total:
        all_amts = main_amounts + right_amounts
        restaurant_total = find_amount_by_keyword_spatial(
            all_amts, ["TOTALES", "TOTAL CONSUMO", "TOTALES:"]
        )

    payment_breakdown = extract_payment_breakdown_spatial(lines)
    cash_amount = payment_breakdown["cash_amount"]
    card_amount = payment_breakdown["card_amount"]

    card_network = (payment_breakdown["card_network_from_payment"] or detect_card_network(normalized))
    card_type = detect_card_type(normalized)
    card_last4 = (payment_breakdown["card_last4_from_payment"] or extract_last4(normalized))

    payment_method = detect_payment_method(normalized, cash_amount, card_amount, card_network, card_last4)

    # ---- IMPORTE DECISION ----
    # Rule: TOTALES from main ticket ALWAYS wins.
    # Fallback: voucher_sale (Venta = consumo base).
    # NEVER use voucher_total (includes propina).
    # NEVER use card_amount or cash_amount as importe (they're partial payments).
    importe = restaurant_total or voucher_sale

    # FALLBACK: if no importe via keywords, use the largest amount found
    if not importe:
        importe = _fallback_largest_amount(lines)
        if importe:
            warnings["importe"] = "fallback_mayor_monto"

    # Sanity check: reject absurd importe values
    if importe is not None and not _is_plausible_amount(importe):
        warnings["importe"] = f"valor_descartado(${importe:,.2f})"
        importe = None

    ticket_date = extract_ticket_date(normalized)
    mesa = extract_mesa_spatial(lines)
    personas = extract_personas_spatial(lines)
    mesero = extract_mesero(normalized)
    voucher_operation = extract_voucher_operation(normalized)

    # Propina cruzada: si es igual al importe, probablemente OCR confundió
    if propina is not None and propina == importe:
        propina = None

    # Per-field warnings
    if not mesa:
        warnings["mesa"] = "no_detectada"
    if not personas:
        warnings["personas"] = "no_detectada"
    if not mesero:
        warnings["mesero"] = "no_detectado"
    if not importe:
        warnings["importe"] = "no_detectado"

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
        "warnings": warnings,
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