from __future__ import annotations

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
    ocr_and_parse,
    parse_tip_reply_message,
)
from .settings import settings
from .telegram_api import download_file_bytes, extract_best_file_id, send_message

app = modal.App("ocr-restaurant-checks")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("tesseract-ocr", "tesseract-ocr-spa")
    .pip_install(
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
    return f"${float(v):.2f}"

def ticket_summary(parsed: dict) -> str:
    mesa = parsed.get("mesa") or "s/d"
    mesero = parsed.get("mesero") or "s/d"
    personas = parsed.get("personas") or "s/d"
    pago = parsed.get("payment_method") or "desconocido"

    lines = [
        f"Mesa: {mesa}",
        f"Mesero: {mesero}",
        f"Personas: {personas}",
        f"Importe total: {fmt_money(parsed.get('importe'))}",
    ]

    if pago == "tarjeta":
        lines.append(
            f"Tarjeta: {fmt_money(parsed.get('card_amount') or parsed.get('importe'))} "
            f"{parsed.get('card_code_sheet') or parsed.get('card_network') or ''} "
            f"{parsed.get('card_last4') or ''}".strip()
        )
    elif pago == "efectivo":
        lines.append(f"Efectivo: {fmt_money(parsed.get('cash_amount') or parsed.get('importe'))}")
    elif pago == "mixto":
        lines.append(
            f"Tarjeta: {fmt_money(parsed.get('card_amount'))} "
            f"{parsed.get('card_code_sheet') or parsed.get('card_network') or ''} "
            f"{parsed.get('card_last4') or ''}".strip()
        )
        lines.append(f"Efectivo: {fmt_money(parsed.get('cash_amount'))}")
    else:
        lines.append(f"Pago: {pago}")

    if parsed.get("propina") not in (None, ""):
        lines.append(f"Propina detectada: {fmt_money(parsed.get('propina'))}")

    return "\n".join(lines)

def process_ticket_message(chat_id: str, reply_to_message_id: int | None, file_id: str) -> None:
    try:
        image_bytes = download_file_bytes(file_id)
        parsed = ocr_and_parse(image_bytes)
    except Exception as e:
        send_message(chat_id, f"No pude descargar o leer la imagen. Error: {e}", reply_to_message_id)
        return

    has_any_amount = any(
        parsed.get(k) not in (None, "", 0, 0.0)
        for k in ["importe", "cash_amount", "card_amount", "total_detected"]
    )

    if not has_any_amount:
        try:
            failed_payload = build_failed_log_payload(chat_id, file_id, parsed)
            ctx = get_runtime(date.fromisoformat(local_today_iso()))
            append_log_record(ctx.log_ws, failed_payload)
        except Exception:
            pass

        send_message(
            chat_id,
            "No pude identificar bien el ticket. Mándame una foto más derecha, completa y con buena luz 🙏",
            reply_to_message_id,
        )
        return

    ticket_date = parsed_ticket_date_or_today(parsed)
    ctx = get_runtime(ticket_date)
    responsable = ctx.config.get("responsable_default", "")

    parsed["card_code_sheet"] = resolve_card_code_sheet(
        ctx.config,
        parsed.get("card_network"),
        parsed.get("card_type"),
    )

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
            "Ojo: este ticket parece duplicado y no lo guardé para no repetirlo 👀",
            reply_to_message_id,
        )
        return

    payment_method = parsed.get("payment_method")

    if payment_method == "tarjeta":
        card_base_amount = parsed.get("card_amount") or parsed.get("voucher_sale") or parsed.get("importe")
        write_payload = {**parsed, "importe": card_base_amount}

        if parsed.get("propina") not in (None, "", 0, 0.0):
            write_payload["tip_in_card"] = float(parsed["propina"])
            write_payload["tip_in_cash"] = None

            target_row, target_table = write_tarjeta(ctx.day_ws, ctx.config, write_payload, responsable)

            log_payload = build_log_payload(
                parsed=write_payload,
                responsable=responsable,
                target_table=target_table,
                target_row=target_row,
                telegram_chat_id=chat_id,
                telegram_file_id=file_id,
                status="SAVED_CARD",
                tip_in_card=float(parsed["propina"]),
                tip_in_cash=None,
                tip_mode_final="card",
            )
            append_log_record(ctx.log_ws, log_payload)

            send_message(
                chat_id,
                "Listo, ticket de tarjeta guardado ✅\n\n" + ticket_summary(write_payload),
                reply_to_message_id,
            )
            return

        write_payload["tip_in_card"] = None
        write_payload["tip_in_cash"] = None
        target_row, target_table = write_tarjeta(ctx.day_ws, ctx.config, write_payload, responsable)

        log_payload = build_log_payload(
            parsed=write_payload,
            responsable=responsable,
            target_table=target_table,
            target_row=target_row,
            telegram_chat_id=chat_id,
            telegram_file_id=file_id,
            status="PENDING_TIP_CARD",
            tip_in_card=None,
            tip_in_cash=None,
            tip_mode_final="",
        )
        append_log_record(ctx.log_ws, log_payload)

        send_message(
            chat_id,
            "Leí ticket con pago con tarjeta, pero no detecté propina.\n\n"
            f"{ticket_summary(write_payload)}\n\n"
            "Respóndeme solo con la propina. Ejemplo: 80 o $80",
            reply_to_message_id,
        )
        return

    if payment_method == "efectivo":
        cash_base_amount = parsed.get("cash_amount") or parsed.get("importe")
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

            send_message(
                chat_id,
                "Listo, ticket en efectivo guardado ✅\n\n" + ticket_summary(write_payload),
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
        append_log_record(ctx.log_ws, log_payload)

        send_message(
            chat_id,
            "Leí ticket en efectivo.\n\n"
            f"{ticket_summary(write_payload)}\n\n"
            "Respóndeme solo con la propina. Ejemplo: 50 o $50",
            reply_to_message_id,
        )
        return

    if payment_method == "mixto":
        card_amount = parsed.get("card_amount")
        cash_amount = parsed.get("cash_amount")

        if card_amount in (None, 0, 0.0) or cash_amount in (None, 0, 0.0):
            send_message(
                chat_id,
                "Detecté que parece ticket mixto, pero no pude separar bien cuánto fue tarjeta y cuánto efectivo. Mándame otra foto porfa 🙏",
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

            send_message(
                chat_id,
                "Listo, ticket mixto guardado ✅\n\n" + ticket_summary(parsed),
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
        append_log_record(ctx.log_ws, log_payload)

        send_message(
            chat_id,
            "Leí un ticket mixto.\n\n"
            f"{ticket_summary(parsed)}\n\n"
            "Respóndeme así:\n"
            "tarjeta 80\n"
            "o\n"
            "efectivo 80",
            reply_to_message_id,
        )
        return

    send_message(
        chat_id,
        "No pude identificar si fue tarjeta, efectivo o mixto. Mándame otra foto más clara porfa 🙏",
        reply_to_message_id,
    )


def process_tip_reply(chat_id: str, reply_to_message_id: int | None, text: str) -> None:
    parsed_tip = parse_tip_reply_message(text)
    if parsed_tip is None:
        send_message(
            chat_id,
            "No pude leer la propina. Respóndeme solo con un monto, por ejemplo: 50 o $50.\n"
            "Si el ticket fue mixto, respóndeme así: tarjeta 50 o efectivo 50",
            reply_to_message_id,
        )
        return

    tip_amount = float(parsed_tip["amount"])
    requested_mode = parsed_tip["mode"]

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

    # volvemos a leer el pendiente pero en la hoja/log del mes correcto
    pending = find_latest_pending_for_chat(ctx.log_ws, chat_id)
    if not pending:
        send_message(
            chat_id,
            "Encontré el ticket, pero no pude recuperar el pendiente en el archivo del mes.",
            reply_to_message_id,
        )
        return

    pending_status = str(pending.get("status", "")).upper()

    if pending_status == "PENDING_TIP_CARD":
        final_mode = "card"
    elif pending_status == "PENDING_TIP_EFECTIVO":
        final_mode = "cash"
    elif pending_status == "PENDING_TIP_MIXTO":
        final_mode = requested_mode
        if final_mode not in {"card", "cash"}:
            send_message(
                chat_id,
                "Como ese ticket fue mixto, necesito que me digas dónde fue la propina.\n"
                "Respóndeme así: tarjeta 80 o efectivo 80",
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
        tip_amount,
        tip_target_mode=final_mode,
    )

    importe = float(str(pending.get("importe") or "0").replace("$", "").replace(",", "") or 0)
    new_status = {
        "card": "COMPLETED_CARD_TIP",
        "cash": "COMPLETED_CASH_TIP",
    }[final_mode]

    updates = {
        "tip_mode_final": final_mode,
        "total_cobrado": round(importe + tip_amount, 2),
        "status": new_status,
    }

    if final_mode == "card":
        updates["tip_in_card"] = round(tip_amount, 2)
    else:
        updates["tip_in_cash"] = round(tip_amount, 2)

    update_log_row(ctx.log_ws, int(pending["_row"]), updates)

    send_message(
        chat_id,
        f"Listo, propina registrada: ${tip_amount:.2f} ✅",
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

@app.function(image=image, timeout=120)
@modal.web_endpoint(method="POST")
def telegram_webhook(update: dict):
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
                "Hola 👋 Mándame una foto del ticket y yo lo intento pasar a la hoja del día.",
                reply_to_message_id,
            )
        else:
            process_tip_reply(chat_id, reply_to_message_id, text)

    return {"ok": True}