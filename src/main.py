from __future__ import annotations

import time
import traceback
from datetime import date

import modal

from src.google_sheets import (
    append_log_record,
    build_log_payload,
    find_latest_pending_for_chat,
    get_runtime,
    is_duplicate,
    resolve_card_code_sheet,
    update_log_row,
    write_efectivo,
    write_propina_tarjeta_efectivo,
    write_tarjeta,
)
from src.ocr_parser import (
    local_today_iso,
    normalize_text,
    ocr_and_parse,
    parse_tip_reply_message,
    resolve_mesero_flexible,
)
from src.settings import settings
from src.telegram_api import download_file_bytes, extract_best_file_id, send_message

app = modal.App("ocr-restaurant-checks")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("tesseract-ocr", "tesseract-ocr-spa")
    .pip_install(
        "fastapi[standard]",
        "gspread",
        "google-api-python-client",
        "google-auth",
        "pytesseract",
        "Pillow",
        "requests",
    )
)


def parsed_ticket_date_or_today(parsed: dict) -> date:
    ticket_date = parsed.get("ticket_date") or local_today_iso()
    return date.fromisoformat(ticket_date)

def fmt_money(v):
    if v in (None, ""):
        return "s/d"
    return f"${float(v):,.2f}"

def ticket_summary(parsed: dict) -> str:
    mesa = parsed.get("mesa")
    mesero = parsed.get("mesero") or "s/d"
    personas = parsed.get("personas") or "s/d"
    pago = parsed.get("payment_method") or "desconocido"
    ticket_date = parsed.get("ticket_date")
    warnings = parsed.get("warnings") or {}

    lines = []

    if ticket_date:
        lines.append(f"📅 Fecha: {ticket_date}")

    if mesa:
        lines.append(f"🪑 Mesa: {mesa}")
    else:
        lines.append("🪑 Mesa: ⚠️ no detectada")

    lines.append(f"👤 Mesero: {mesero}")
    lines.append(f"👥 Personas: {personas}")
    lines.append(f"💰 Importe: {fmt_money(parsed.get('importe'))}")

    if pago == "tarjeta":
        card_info = f"{parsed.get('card_code_sheet') or parsed.get('card_network') or ''} {parsed.get('card_last4') or ''}".strip()
        lines.append(
            f"💳 Tarjeta: {fmt_money(parsed.get('card_amount') or parsed.get('importe'))}"
            + (f" ({card_info})" if card_info else "")
        )
    elif pago == "efectivo":
        lines.append(f"💵 Efectivo: {fmt_money(parsed.get('cash_amount') or parsed.get('importe'))}")
    elif pago == "mixto":
        card_info = f"{parsed.get('card_code_sheet') or parsed.get('card_network') or ''} {parsed.get('card_last4') or ''}".strip()
        lines.append(
            f"💳 Tarjeta: {fmt_money(parsed.get('card_amount'))}"
            + (f" ({card_info})" if card_info else "")
        )
        lines.append(f"💵 Efectivo: {fmt_money(parsed.get('cash_amount'))}")
    else:
        lines.append(f"Pago: {pago}")

    if parsed.get("propina") not in (None, "", 0, 0.0):
        lines.append(f"🎁 Propina: {fmt_money(parsed.get('propina'))}")

    # Show warnings if any
    if warnings:
        warn_parts = []
        for field, msg in warnings.items():
            warn_parts.append(f"{field}: {msg}")
        lines.append(f"⚠️ Notas: {', '.join(warn_parts)}")

    return "\n".join(lines)

def process_ticket_message(chat_id: str, reply_to_message_id: int | None, file_id: str) -> None:
    t0 = time.time()

    # Feedback inmediato para que el usuario sepa que recibimos la foto
    send_message(chat_id, "⏳ Procesando ticket…", reply_to_message_id)

    try:
        t1 = time.time()
        image_bytes = download_file_bytes(file_id)
        t2 = time.time()
        parsed = ocr_and_parse(image_bytes)
        t3 = time.time()
        print(f"[TIMING] download={t2-t1:.2f}s  ocr={t3-t2:.2f}s")
    except Exception as e:
        send_message(chat_id, f"⚠️ No pude descargar o leer la imagen.\nError: {e}", reply_to_message_id)
        return

    has_any_amount = any(
        parsed.get(k) not in (None, "", 0, 0.0)
        for k in ["importe", "cash_amount", "card_amount", "total_detected"]
    )

    has_any_data = has_any_amount or parsed.get("mesa") or parsed.get("personas") or parsed.get("mesero")

    if not has_any_data:
        # Truly nothing usable — but still log it
        try:
            failed_payload = build_failed_log_payload(chat_id, file_id, parsed)
            ctx = get_runtime(date.fromisoformat(local_today_iso()))
            append_log_record(ctx.log_ws, failed_payload)
        except Exception:
            pass

        send_message(
            chat_id,
            "⚠️ No pude extraer datos útiles de esta imagen.\n"
            "Intenta con foto más derecha, completa y con buena luz 📸",
            reply_to_message_id,
        )
        return

    ticket_date = parsed_ticket_date_or_today(parsed)
    parsed["ticket_date"] = ticket_date.isoformat()

    t4 = time.time()
    ctx = get_runtime(ticket_date)
    t5 = time.time()
    print(f"[TIMING] sheets_init={t5-t4:.2f}s")

    responsable = ctx.config.get("responsable_default", "MGVR")

    parsed["card_code_sheet"] = resolve_card_code_sheet(
        ctx.config,
        parsed.get("card_network"),
        parsed.get("card_type"),
    )

    # --- Flexible mesero resolution using CONFIG aliases ---
    mesero_resolved, mesero_warning = resolve_mesero_flexible(
        parsed.get("mesero"), ctx.config
    )
    if mesero_resolved:
        parsed["mesero"] = mesero_resolved
    if mesero_warning:
        warnings = parsed.get("warnings") or {}
        warnings["mesero"] = mesero_warning
        parsed["warnings"] = warnings

    # Validation para Tarjetas basado en CONFIG
    tarjetas_str = ctx.config.get("tarjetas_validas", "")
    if tarjetas_str and not parsed.get("card_network"):
        valid_cards = [c.strip().upper() for c in tarjetas_str.split(",") if c.strip()]
        text_norm = normalize_text(parsed.get("ocr_raw_text", ""))
        for vc in valid_cards:
            if vc in text_norm:
                parsed["card_network"] = vc.lower()
                break

    # Propina sanity check: warn but keep value (only null if truly absurd)
    propina_val = parsed.get("propina")
    if propina_val is not None and propina_val > 0:
        importe_val = parsed.get("importe") or 0.0
        if propina_val > 9999.99:
            parsed["propina"] = None  # truly absurd
        elif propina_val > 999.99 or (importe_val > 0 and propina_val > importe_val):
            warnings = parsed.get("warnings") or {}
            warnings["propina"] = f"valor_alto(${propina_val:,.2f})"
            parsed["warnings"] = warnings
            # Keep the value — user can review


    if is_duplicate(ctx.log_ws, ctx.config, parsed):
        log_payload = build_log_payload(
            parsed=parsed,
            responsable=responsable,
            target_table="",
            target_row="",
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="DUPLICATE_SKIPPED",
        )
        append_log_record(ctx.log_ws, log_payload)

        send_message(
            chat_id,
            "⚠️ Ticket duplicado — no guardado.\n\n" + ticket_summary(parsed),
            reply_to_message_id,
        )
        return

    payment_method = parsed.get("payment_method")

    if payment_method == "tarjeta":
        card_base_amount = parsed.get("importe")
        write_payload = {**parsed, "importe": card_base_amount, "tip_in_card": None, "tip_in_cash": None}

        # Siempre preguntamos si la propina fue en tarjeta o en efectivo (Escenario 1 vs Escenario 2)
        target_row, target_table = write_tarjeta(ctx.day_ws, ctx.config, write_payload, responsable)

        propina_ocr = parsed.get("propina")

        log_payload = build_log_payload(
            parsed=write_payload,
            responsable=responsable,
            target_table=target_table,
            target_row=target_row,
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="PENDING_TIP_CARD_TYPE",
            tip_in_card=propina_ocr,  # Guardamos temporalmente el valor del OCR
            tip_in_cash=None,
            tip_mode_final="",
        )
        log_row = append_log_record(ctx.log_ws, log_payload)

        try:
            pending_dict = modal.Dict.from_name("ocr-bot-pending-tips", create_if_missing=True)
            pending_dict[chat_id] = {
                "ticket_date": parsed["ticket_date"],
                "target_table": target_table,
                "target_row": target_row,
                "_row": log_row,
                "status": "PENDING_TIP_CARD_TYPE",
                "importe": card_base_amount,
                "tip_in_card": propina_ocr,
                "parsed": parsed
            }
        except Exception as e:
            print("Error parsing state to Modal Dict:", e)

        if propina_ocr not in (None, "", 0, 0.0):
            msg = (
                f"📋 Detecté un pago con tarjeta.\n"
                f"El monto de propina detectado es de {fmt_money(propina_ocr)}.\n\n"
                f"¿La propina fue en tarjeta o en efectivo?\n"
                f"(Responde 'tarjeta' o 'efectivo', o con monto ej: 'efectivo 50')"
            )
        else:
            msg = (
                f"📋 Detecté un pago con tarjeta.\n\n"
                f"¿La propina fue en tarjeta o en efectivo y de cuánto fue?\n"
                f"(Ej: 'tarjeta 50' o 'efectivo 50')"
            )

        t6 = time.time()
        print(f"[TIMING] total={t6-t0:.2f}s")
        send_message(
            chat_id,
            msg,
            reply_to_message_id,
        )
        return

    if payment_method == "efectivo":
        cash_base_amount = parsed.get("importe")
        write_payload = {**parsed, "importe": cash_base_amount}

        if parsed.get("propina") not in (None, "", 0, 0.0):
            write_payload["tip_in_cash"] = float(parsed["propina"])
            target_row, target_table = write_efectivo(ctx.day_ws, ctx.config, write_payload, responsable)

            log_payload = build_log_payload(
                parsed=write_payload,
                responsable=responsable,
                target_table=target_table,
                target_row=target_row,
                telegram_chat_id=chat_id,
                telegram_file_id=file_id,
                status="SAVED_CASH",
                tip_in_card=None,
                tip_in_cash=float(parsed["propina"]),
                tip_mode_final="cash",
            )
            append_log_record(ctx.log_ws, log_payload)

            t6 = time.time()
            print(f"[TIMING] total={t6-t0:.2f}s")
            send_message(
                chat_id,
                "✅ Ticket registrado en Efectivo\n\n" + ticket_summary(write_payload),
                reply_to_message_id,
            )
            return

        write_payload["tip_in_cash"] = None
        target_row, target_table = write_efectivo(ctx.day_ws, ctx.config, write_payload, responsable)

        log_payload = build_log_payload(
            parsed=write_payload,
            responsable=responsable,
            target_table=target_table,
            target_row=target_row,
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="PENDING_TIP_EFECTIVO",
            tip_in_card=None,
            tip_in_cash=None,
            tip_mode_final="",
        )
        log_row = append_log_record(ctx.log_ws, log_payload)

        try:
            pending_dict = modal.Dict.from_name("ocr-bot-pending-tips", create_if_missing=True)
            pending_dict[chat_id] = {
                "ticket_date": parsed["ticket_date"],
                "target_table": target_table,
                "target_row": target_row,
                "_row": log_row,
                "status": "PENDING_TIP_EFECTIVO",
                "importe": cash_base_amount,
                "tip_in_cash": None,
                "parsed": parsed
            }
        except Exception as e:
            print("Error parsing state to Modal Dict:", e)

        t6 = time.time()
        print(f"[TIMING] total={t6-t0:.2f}s")
        send_message(
            chat_id,
            "📋 Detecté pago en efectivo.\n\n"
            f"{ticket_summary(write_payload)}\n"
            "💬 ¿Cuánto fue de efectivo (propina)? (Ej: 50)",
            reply_to_message_id,
        )
        return

    if payment_method == "mixto":
        card_amount = parsed.get("card_amount")
        cash_amount = parsed.get("cash_amount")

        if card_amount in (None, 0, 0.0) or cash_amount in (None, 0, 0.0):
            send_message(
                chat_id,
                "⚠️ Parece ticket mixto, pero no pude separar tarjeta/efectivo.\n"
                "Manda otra foto más clara 📸",
                reply_to_message_id,
            )
            return

        card_payload = {**parsed, "importe": card_amount, "tip_in_card": None}
        cash_payload = {**parsed, "importe": cash_amount, "tip_in_cash": None}

        if parsed.get("propina") not in (None, "", 0, 0.0):
            card_payload["tip_in_card"] = float(parsed["propina"])

        card_row, _ = write_tarjeta(ctx.day_ws, ctx.config, card_payload, responsable)
        cash_row, _ = write_efectivo(ctx.day_ws, ctx.config, cash_payload, responsable)

        if parsed.get("propina") not in (None, "", 0, 0.0):
            log_payload = build_log_payload(
                parsed=parsed,
                responsable=responsable,
                target_table="mixto",
                target_row=f"{card_row}|{cash_row}",
                telegram_chat_id=chat_id,
                telegram_file_id=file_id,
                status="SAVED_MIXED",
                tip_in_card=float(parsed["propina"]),
                tip_in_cash=None,
                tip_mode_final="card",
            )
            append_log_record(ctx.log_ws, log_payload)

            t6 = time.time()
            print(f"[TIMING] total={t6-t0:.2f}s")
            send_message(
                chat_id,
                "✅ Ticket mixto registrado\n\n" + ticket_summary(parsed),
                reply_to_message_id,
            )
            return

        log_payload = build_log_payload(
            parsed=parsed,
            responsable=responsable,
            target_table="mixto",
            target_row=f"{card_row}|{cash_row}",
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="PENDING_TIP_MIXTO",
            tip_in_card=None,
            tip_in_cash=None,
            tip_mode_final="",
        )
        log_row = append_log_record(ctx.log_ws, log_payload)

        try:
            pending_dict = modal.Dict.from_name("ocr-bot-pending-tips", create_if_missing=True)
            pending_dict[chat_id] = {
                "ticket_date": parsed["ticket_date"],
                "target_table": "mixto",
                "target_row": f"{card_row}|{cash_row}",
                "_row": log_row,
                "status": "PENDING_TIP_MIXTO",
                "importe": card_amount,
                "parsed": parsed
            }
        except Exception as e:
            print("Error parsing state to Modal Dict:", e)

        t6 = time.time()
        print(f"[TIMING] total={t6-t0:.2f}s")
        send_message(
            chat_id,
            "📋 Ticket mixto registrado — falta propina\n\n"
            f"{ticket_summary(parsed)}\n\n"
            "💬 Responde así:\n"
            "tarjeta 80\n"
            "o\n"
            "efectivo 80",
            reply_to_message_id,
        )
        return

    send_message(
        chat_id,
        "⚠️ No identifiqué el método de pago con certeza.\n"
        "Registré los datos que sí pude extraer:\n\n" + ticket_summary(parsed),
        reply_to_message_id,
    )


def process_tip_reply(chat_id: str, reply_to_message_id: int | None, text: str) -> None:
    parsed_tip = parse_tip_reply_message(text)
    if parsed_tip is None:
        send_message(
            chat_id,
            "💬 No entendí el mensaje.\n"
            "Si es tarjeta: 'tarjeta 50' o 'efectivo 50'.\n"
            "Si es solo monto: '50'.",
            reply_to_message_id,
        )
        return

    tip_amount_raw = parsed_tip.get("amount")
    requested_mode = parsed_tip.get("mode")

    try:
        pending_dict = modal.Dict.from_name("ocr-bot-pending-tips", create_if_missing=True)
        pending = pending_dict.get(chat_id)
    except Exception as e:
        print("Error reading Modal Dict:", e)
        pending = None

    if not pending:
        send_message(
            chat_id,
            "💬 No encontré una operación pendiente asociada a tu chat.\nEs posible que ya se haya cerrado o guardado.",
            reply_to_message_id,
        )
        return

    pending_ticket_date = date.fromisoformat(pending["ticket_date"])
    ctx = get_runtime(pending_ticket_date)

    pending_status = str(pending.get("status", "")).upper()

    final_amount = tip_amount_raw
    final_mode = None

    if pending_status == "PENDING_TIP_CARD_TYPE":
        final_mode = requested_mode
        if not final_mode:
            send_message(
                chat_id,
                "💬 Por favor especifica si la propina fue en 'tarjeta' o en 'efectivo'.",
                reply_to_message_id,
            )
            return

        if final_amount is None:
            # Try to grab OCR tip stored temporarily in tip_in_card
            ocr_tip_raw = pending.get("tip_in_card") or ""
            ocr_tip_str = str(ocr_tip_raw).replace("$", "").replace(",", "").strip()
            try:
                final_amount = float(ocr_tip_str) if ocr_tip_str else None
            except Exception:
                final_amount = None

            if final_amount is None or final_amount <= 0:
                send_message(
                    chat_id,
                    "💬 No encontré el monto de la propina.\nResponde con el monto y tipo de propina (ej: 'tarjeta 50' o 'efectivo 50').",
                    reply_to_message_id,
                )
                return

    elif pending_status == "PENDING_TIP_EFECTIVO":
        if final_amount is None:
            send_message(
                chat_id,
                "💬 Necesito el monto numérico. (ej: 50)",
                reply_to_message_id,
            )
            return
        final_mode = "cash"

    elif pending_status == "PENDING_TIP_MIXTO":
        final_mode = requested_mode
        if final_mode not in {"card", "cash"}:
            send_message(
                chat_id,
                "💬 Ese ticket fue mixto. Dime dónde fue la propina:\n"
                "tarjeta 80 o efectivo 80",
                reply_to_message_id,
            )
            return
        if final_amount is None:
             send_message(
                chat_id,
                "💬 Necesito el monto numérico para el ticket mixto. (ej: tarjeta 80)",
                reply_to_message_id,
            )
             return
    else:
        send_message(
            chat_id,
            "Ese ticket ya no está pendiente de propina.",
            reply_to_message_id,
        )
        return

    write_propina_tarjeta_efectivo(
        ctx.day_ws,
        ctx.config,
        pending,
        final_amount,
        tip_target_mode=final_mode,
    )

    importe = float(str(pending.get("importe") or "0").replace("$", "").replace(",", "") or 0)
    new_status = {
        "card": "COMPLETED_CARD_TIP",
        "cash": "COMPLETED_CASH_TIP",
    }[final_mode]

    updates = {
        "tip_mode_final": final_mode,
        "total_cobrado": round(importe + final_amount, 2),
        "status": new_status,
    }

    if final_mode == "card":
        updates["tip_in_card"] = round(final_amount, 2)
    else:
        updates["tip_in_cash"] = round(final_amount, 2)

    update_log_row(ctx.log_ws, int(pending["_row"]), updates)

    try:
        pending_dict = modal.Dict.from_name("ocr-bot-pending-tips", create_if_missing=True)
        # Limpieza correcta del estado pendiente
        if chat_id in pending_dict:
            pending_dict.pop(chat_id)
    except Exception as e:
        print("Error clearing Modal Dict:", e)

    send_message(
        chat_id,
        f"✅ Propina registrada: ${final_amount:,.2f} en {final_mode}",
        reply_to_message_id,
    )
    
    # Extraer variables para el resumen final conversacional
    past_parsed = pending.get("parsed", {})
    if past_parsed:
        if final_mode == "cash": past_parsed["tip_in_cash"] = final_amount
        if final_mode == "card": past_parsed["tip_in_card"] = final_amount
        past_parsed["propina"] = final_amount
        
        send_message(
            chat_id,
            "📝 Resumen de la Operación Cerrada:\n\n" + ticket_summary(past_parsed),
            reply_to_message_id,
        )


def build_failed_log_payload(chat_id: str, file_id: str, parsed: dict | None = None) -> dict:
    today = local_today_iso()
    base = parsed or {}
    fallback = {
        "ticket_date": today,
        "payment_method": base.get("payment_method", ""),
        "mesa": base.get("mesa", ""),
        "mesero": base.get("mesero", ""),
        "personas": base.get("personas", ""),
        "importe": base.get("importe", 0),
        "card_network": base.get("card_network", ""),
        "card_type": base.get("card_type", ""),
        "card_code_sheet": base.get("card_code_sheet", ""),
        "card_last4": base.get("card_last4", ""),
        "voucher_operation": base.get("voucher_operation", ""),
        "ocr_raw_text": base.get("ocr_raw_text", ""),
    }
    ctx = get_runtime(date.fromisoformat(today))
    responsable = ctx.config.get("responsable_default", "")
    return build_log_payload(
        parsed=fallback,
        responsable=responsable,
        target_table="",
        target_row="",
        telegram_chat_id=chat_id,
        telegram_file_id=file_id,
        status="OCR_FAILED",
    )


modal_secrets = [
    modal.Secret.from_name("castillo-bot-secrets")
]
@app.function(image=image, timeout=120, secrets=modal_secrets)
@modal.web_endpoint(method="POST")
def telegram_webhook(update: dict):
    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"ok": True}

        chat_id = str(message["chat"]["id"])
        reply_to_message_id = message.get("message_id")

        file_id = extract_best_file_id(message)
        if file_id:
            process_ticket_message(chat_id, reply_to_message_id, file_id)
            return {"ok": True}

        text = (message.get("text") or "").strip()
        if text:
            if text.startswith("/start"):
                send_message(
                    chat_id,
                    "👋 ¡Hola! Mándame una foto del ticket y lo paso a la hoja del día.",
                    reply_to_message_id,
                )
            else:
                process_tip_reply(chat_id, reply_to_message_id, text)

        return {"ok": True}

    except Exception as e:
        print("ERROR EN WEBHOOK")
        print(repr(e))
        print(traceback.format_exc())
        print("UPDATE RECIBIDO:", update)
        return {"ok": True, "error": str(e)}