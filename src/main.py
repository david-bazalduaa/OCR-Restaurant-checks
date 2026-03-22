from __future__ import annotations

import time
import traceback
from datetime import date

import modal

from .google_sheets import (
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
from .ocr_parser import (
    local_today_iso,
    normalize_text,
    ocr_and_parse,
    parse_tip_reply_message,
    resolve_mesero_flexible,
)
from .settings import settings
from .telegram_api import download_file_bytes, extract_best_file_id, send_message

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

    # Guardado diferido: No salvamos en las tablas principales durante esta fase si falta información.
    if payment_method == "tarjeta":
        propina_val = parsed.get("propina")
        
        # Guardamos en LOG como pendiente
        log_payload = build_log_payload(
            parsed=parsed,
            responsable=responsable,
            target_table="DEFERRED_CARD",
            target_row=str(propina_val) if propina_val else "",
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="PENDING_TIP_TARJETA_MODE",
        )
        append_log_record(ctx.log_ws, log_payload)

        msg = "💳 Detecté un pago con tarjeta.\n"
        if propina_val:
            msg += f"Propina sugerida: ${propina_val:,.2f}\n"
        msg += "¿La propina fue en tarjeta o en efectivo?\n(Responde ej: 'tarjeta', 'efectivo', 'tarjeta 50')"
            
        send_message(chat_id, msg, reply_to_message_id)
        return

    if payment_method == "efectivo":
        importe = parsed.get("importe")
        propina_val = parsed.get("propina")
        
        if not importe:
            log_payload = build_log_payload(
                parsed=parsed,
                responsable=responsable,
                target_table="DEFERRED_CASH",
                target_row=str(propina_val) if propina_val else "",
                telegram_chat_id=chat_id,
                telegram_file_id=file_id,
                status="PENDING_CASH_AMOUNT",
            )
            append_log_record(ctx.log_ws, log_payload)
            send_message(
                chat_id,
                "💵 Detecté pago en efectivo, pero no pude leer el importe total consumido.\n¿Cuánto fue de efectivo total (sin propina)?\n(ej: '500')",
                reply_to_message_id
            )
            return
            
        # Si tenemos el importe, lo guardamos directo
        write_payload = {**parsed, "importe": importe, "tip_in_cash": propina_val}
        row, target_table = write_efectivo(ctx.day_ws, ctx.config, write_payload, responsable)
        
        log_payload = build_log_payload(
            parsed=write_payload,
            responsable=responsable,
            target_table=target_table,
            target_row=row,
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="SAVED_CASH",
            tip_in_cash=propina_val,
            tip_mode_final="cash"
        )
        append_log_record(ctx.log_ws, log_payload)
        send_message(chat_id, "✅ Ticket registrado en Efectivo\n\n" + ticket_summary(write_payload), reply_to_message_id)
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

        propina_val = parsed.get("propina")
        
        log_payload = build_log_payload(
            parsed=parsed,
            responsable=responsable,
            target_table="DEFERRED_MIXTO",
            target_row=f"{propina_val or ''}|{card_amount}|{cash_amount}",
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="PENDING_TIP_MIXTO",
        )
        append_log_record(ctx.log_ws, log_payload)
        
        msg = f"💳💵 Ticket mixto (Tarjeta: ${card_amount}, Efectivo: ${cash_amount}).\n"
        if propina_val:
            msg += f"Propina sugerida: ${propina_val:,.2f}\n"
        msg += "¿La propina fue en tarjeta o en efectivo?\n(Responde ej: 'tarjeta', 'efectivo', 'tarjeta 50')"
        send_message(chat_id, msg, reply_to_message_id)
        return

    send_message(
        chat_id,
        "⚠️ No identifiqué el método de pago con certeza.\n"
        "Registré los datos que sí pude extraer:\n\n" + ticket_summary(parsed),
        reply_to_message_id,
    )


def process_tip_reply(chat_id: str, reply_to_message_id: int | None, text: str) -> None:
    parsed_tip = parse_tip_reply_message(text)
    
    today = date.fromisoformat(local_today_iso())
    ctx_today = get_runtime(today)
    pending = find_latest_pending_for_chat(ctx_today.log_ws, chat_id)

    if not pending:
        send_message(
            chat_id,
            "No encontré un ticket pendiente de propina en este mes.",
            reply_to_message_id,
        )
        return

    pending_ticket_date = date.fromisoformat(pending["ticket_date"])
    ctx = get_runtime(pending_ticket_date)

    pending = find_latest_pending_for_chat(ctx.log_ws, chat_id)
    if not pending:
        send_message(
            chat_id,
            "Encontré el ticket, pero no pude recuperar el pendiente en el archivo del mes.",
            reply_to_message_id,
        )
        return

    pending_status = str(pending.get("status", "")).upper()
    stored_target_row = str(pending.get("target_row", "")).strip()

    # Reconstruct the base payload needed for saving
    reconstructed = {
        "payment_method": pending.get("payment_method"),
        "mesa": pending.get("mesa"),
        "mesero": pending.get("mesero"),
        "personas": pending.get("personas"),
        "importe": float(str(pending.get("importe") or "0").replace("$", "").replace(",", "") or 0),
        "card_network": pending.get("card_network"),
        "card_type": pending.get("card_type"),
        "card_code_sheet": pending.get("card_code_sheet"),
        "card_last4": pending.get("card_last4"),
        "ticket_date": pending.get("ticket_date"),
    }
    responsable = pending.get("responsable", "")

    if pending_status == "PENDING_CASH_AMOUNT":
        if not parsed_tip or parsed_tip["amount"] is None:
            send_message(chat_id, "💬 Por favor responde solo con el monto del efectivo (ej: 500).", reply_to_message_id)
            return
            
        importe_val = parsed_tip["amount"]
        reconstructed["importe"] = importe_val
        
        propina_val = None
        if stored_target_row:
            try: propina_val = float(stored_target_row)
            except: pass
            
        reconstructed["tip_in_cash"] = propina_val
        row, target_table = write_efectivo(ctx.day_ws, ctx.config, reconstructed, responsable)
        
        updates = {
            "status": "SAVED_CASH",
            "importe": round(float(importe_val), 2),
            "total_cobrado": round(importe_val + (propina_val or 0.0), 2),
            "target_table": target_table,
            "target_row": row,
        }
        if propina_val:
            updates["tip_in_cash"] = round(float(propina_val), 2)
            updates["tip_mode_final"] = "cash"
            
        update_log_row(ctx.log_ws, int(pending["_row"]), updates)
        send_message(chat_id, "✅ Efectivo registrado\n\n" + ticket_summary(reconstructed), reply_to_message_id)
        return

    # Handle CARD and MIXTO deferred tips
    mode = None
    if parsed_tip and parsed_tip["mode"]:
        mode = parsed_tip["mode"]
    else:
        norm_text = normalize_text(text)
        if "TARJETA" in norm_text or "CARD" in norm_text: mode = "card"
        elif "EFECTIVO" in norm_text or "CASH" in norm_text: mode = "cash"

    tip_amount = None
    if parsed_tip and parsed_tip["amount"] is not None:
        tip_amount = parsed_tip["amount"]

    if tip_amount == 0.0:
        mode = mode or "card"  # arbitrary default if 0 tip

    if not mode:
        send_message(chat_id, "💬 Por favor indica si la propina fue 'en tarjeta' o 'en efectivo'.", reply_to_message_id)
        return

    if pending_status == "PENDING_TIP_TARJETA_MODE":
        if tip_amount is None and stored_target_row:
            try: tip_amount = float(stored_target_row)
            except: pass
            
        if tip_amount is None:
            send_message(chat_id, "💬 No detecté un monto de propina. Por favor incluye el monto (ej: 'tarjeta 50' o 'efectivo 50').", reply_to_message_id)
            return

        if mode == "card":
            reconstructed["tip_in_card"] = tip_amount
            reconstructed["tip_in_cash"] = None
        else:
            reconstructed["tip_in_card"] = None
            reconstructed["tip_in_cash"] = tip_amount

        row, target_table = write_tarjeta(ctx.day_ws, ctx.config, reconstructed, responsable)

        importe_val = reconstructed["importe"]
        updates = {
            "status": "SAVED_CARD",
            "target_table": target_table,
            "target_row": row,
            "tip_mode_final": mode,
            "total_cobrado": round(importe_val + tip_amount, 2),
        }
        if mode == "card":
            updates["tip_in_card"] = round(float(tip_amount), 2)
        else:
            updates["tip_in_cash"] = round(float(tip_amount), 2)
            
        update_log_row(ctx.log_ws, int(pending["_row"]), updates)
        send_message(chat_id, "✅ Tarjeta registrada\n\n" + ticket_summary(reconstructed), reply_to_message_id)
        return

    if pending_status == "PENDING_TIP_MIXTO":
        parts = stored_target_row.split("|")
        stored_propina = parts[0] if len(parts) > 0 else ""
        stored_card = parts[1] if len(parts) > 1 else "0"
        stored_cash = parts[2] if len(parts) > 2 else "0"
        
        if tip_amount is None and stored_propina:
            try: tip_amount = float(stored_propina)
            except: pass
            
        if tip_amount is None:
            send_message(chat_id, "💬 No detecté el monto. Por favor incluye el monto: 'tarjeta 50' o 'efectivo 50'.", reply_to_message_id)
            return

        try:
            card_amount = float(stored_card)
            cash_amount = float(stored_cash)
        except:
            card_amount = reconstructed["importe"] / 2
            cash_amount = reconstructed["importe"] / 2

        card_payload = {**reconstructed, "importe": card_amount, "tip_in_card": None, "tip_in_cash": None}
        cash_payload = {**reconstructed, "importe": cash_amount, "tip_in_cash": None, "tip_in_card": None}

        if mode == "card":
            card_payload["tip_in_card"] = tip_amount
        else:
            cash_payload["tip_in_cash"] = tip_amount

        card_row, _ = write_tarjeta(ctx.day_ws, ctx.config, card_payload, responsable)
        cash_row, _ = write_efectivo(ctx.day_ws, ctx.config, cash_payload, responsable)

        updates = {
            "status": "SAVED_MIXED",
            "target_table": "mixto",
            "target_row": f"{card_row}|{cash_row}",
            "tip_mode_final": mode,
            "total_cobrado": round(card_amount + cash_amount + tip_amount, 2),
        }
        if mode == "card":
            updates["tip_in_card"] = round(float(tip_amount), 2)
        else:
            updates["tip_in_cash"] = round(float(tip_amount), 2)
            
        update_log_row(ctx.log_ws, int(pending["_row"]), updates)
        
        # reconstruct for summary print
        summary_payload = {**reconstructed, "card_amount": card_amount, "cash_amount": cash_amount, "payment_method": "mixto"}
        if mode == "card":
            summary_payload["tip_in_card"] = tip_amount
            summary_payload["propina"] = tip_amount
        else:
            summary_payload["tip_in_cash"] = tip_amount
            summary_payload["propina"] = tip_amount
            
        send_message(chat_id, "✅ Ticket mixto registrado\n\n" + ticket_summary(summary_payload), reply_to_message_id)
        return

    send_message(
        chat_id,
        "Ese ticket ya no está pendiente de propina.",
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