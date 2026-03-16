from datetime import datetime

from src.google_sheets import GoogleSheetsRepository


@app.function(
    image=image.pip_install(
        "google-api-python-client",
        "google-auth",
    ),
    secrets=[modal.Secret.from_name("castillo-bot-secrets")],
)
def google_sheets_smoke_test(date_iso: str = "2026-03-14"):
    settings = Settings.from_env()
    repo = GoogleSheetsRepository(
        service_account_json=settings.google_service_account_json,
        year_folder_id=settings.google_year_folder_id,
    )

    ticket_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
    return repo.healthcheck_for_date(ticket_date)