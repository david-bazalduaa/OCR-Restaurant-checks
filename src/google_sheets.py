import json
from datetime import date
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build


MONTHS_ES = {
    1: "Enero",
    2: "Febrero",
    3: "Marzo",
    4: "Abril",
    5: "Mayo",
    6: "Junio",
    7: "Julio",
    8: "Agosto",
    9: "Septiembre",
    10: "Octubre",
    11: "Noviembre",
    12: "Diciembre",
}

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleSheetsRepository:
    def __init__(self, service_account_json: str, year_folder_id: str):
        self.year_folder_id = year_folder_id

        info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=SCOPES,
        )

        self.drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def month_name_from_date(self, ticket_date: date) -> str:
        return MONTHS_ES[ticket_date.month]

    def day_sheet_name_from_date(self, ticket_date: date) -> str:
        return str(ticket_date.day)

    def find_month_spreadsheet(self, ticket_date: date) -> dict[str, Any] | None:
        month_name = self.month_name_from_date(ticket_date)
        safe_name = _escape_drive_query_value(month_name)

        query = (
            f"'{self.year_folder_id}' in parents "
            f"and mimeType='application/vnd.google-apps.spreadsheet' "
            f"and name='{safe_name}' "
            f"and trashed=false"
        )

        response = self.drive.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=10,
        ).execute()

        files = response.get("files", [])
        return files[0] if files else None

    def create_month_spreadsheet(self, ticket_date: date) -> dict[str, Any]:
        month_name = self.month_name_from_date(ticket_date)

        created = self.sheets.spreadsheets().create(
            body={"properties": {"title": month_name}}
        ).execute()

        spreadsheet_id = created["spreadsheetId"]

        current_meta = self.drive.files().get(
            fileId=spreadsheet_id,
            fields="parents",
        ).execute()

        previous_parents = ",".join(current_meta.get("parents", []))

        self.drive.files().update(
            fileId=spreadsheet_id,
            addParents=self.year_folder_id,
            removeParents=previous_parents,
            fields="id, parents",
        ).execute()

        return {"id": spreadsheet_id, "name": month_name}

    def get_or_create_month_spreadsheet(self, ticket_date: date) -> dict[str, Any]:
        existing = self.find_month_spreadsheet(ticket_date)
        if existing:
            return existing
        return self.create_month_spreadsheet(ticket_date)

    def get_spreadsheet_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        return self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id
        ).execute()

    def find_sheet_id(self, spreadsheet_id: str, sheet_name: str) -> int | None:
        metadata = self.get_spreadsheet_metadata(spreadsheet_id)
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == sheet_name:
                return props.get("sheetId")
        return None

    def ensure_day_sheet(
        self,
        spreadsheet_id: str,
        day_sheet_name: str,
        plantilla_sheet_name: str = "PLANTILLA",
    ) -> int:
        existing_sheet_id = self.find_sheet_id(spreadsheet_id, day_sheet_name)
        if existing_sheet_id is not None:
            return existing_sheet_id

        plantilla_sheet_id = self.find_sheet_id(spreadsheet_id, plantilla_sheet_name)
        if plantilla_sheet_id is None:
            raise ValueError(
                f"No encontré la hoja plantilla '{plantilla_sheet_name}' en el archivo."
            )

        response = self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "duplicateSheet": {
                            "sourceSheetId": plantilla_sheet_id,
                            "newSheetName": day_sheet_name,
                        }
                    }
                ]
            },
        ).execute()

        replies = response.get("replies", [])
        return replies[0]["duplicateSheet"]["properties"]["sheetId"]

    def read_config(
        self,
        spreadsheet_id: str,
        config_sheet_name: str = "CONFIG",
    ) -> dict[str, str]:
        response = self.sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{config_sheet_name}!A2:C100",
        ).execute()

        rows = response.get("values", [])
        config: dict[str, str] = {}

        for row in rows:
            if len(row) >= 2 and row[0]:
                config[row[0]] = row[1]

        return config

    def next_empty_row(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        column: str,
        start_row: int,
        end_row: int,
    ) -> int:
        response = self.sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!{column}{start_row}:{column}{end_row}",
        ).execute()

        rows = response.get("values", [])

        for offset in range(end_row - start_row + 1):
            row_number = start_row + offset
            value = ""

            if offset < len(rows) and rows[offset]:
                value = str(rows[offset][0]).strip()

            if value == "":
                return row_number

        raise ValueError(
            f"No encontré filas vacías en {sheet_name}!{column}{start_row}:{column}{end_row}"
        )

    def healthcheck_for_date(self, ticket_date: date) -> dict[str, Any]:
        spreadsheet = self.get_or_create_month_spreadsheet(ticket_date)
        spreadsheet_id = spreadsheet["id"]
        day_sheet_name = self.day_sheet_name_from_date(ticket_date)

        config = self.read_config(spreadsheet_id)
        plantilla_sheet_name = config.get("plantilla_sheet_name", "PLANTILLA")

        day_sheet_id = self.ensure_day_sheet(
            spreadsheet_id=spreadsheet_id,
            day_sheet_name=day_sheet_name,
            plantilla_sheet_name=plantilla_sheet_name,
        )

        tarjeta_start_row = int(config.get("tarjeta_start_row", "8"))
        tarjeta_end_row = int(config.get("tarjeta_end_row", "30"))
        tarjeta_next_row = self.next_empty_row(
            spreadsheet_id=spreadsheet_id,
            sheet_name=day_sheet_name,
            column="A",
            start_row=tarjeta_start_row,
            end_row=tarjeta_end_row,
        )

        return {
            "spreadsheet_id": spreadsheet_id,
            "month_name": spreadsheet["name"],
            "day_sheet_name": day_sheet_name,
            "day_sheet_id": day_sheet_id,
            "config_loaded": True,
            "tarjeta_next_row": tarjeta_next_row,
        }