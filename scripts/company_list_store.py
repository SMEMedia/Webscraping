"""Read and write the canonical company list in Google Sheets and local JSON."""

import json
from datetime import datetime, timezone
from pathlib import Path

import gspread
import requests
from google.auth.transport.requests import Request


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "scripts" else SCRIPT_DIR
COMPANY_NAMES_FILE = BASE_DIR / "config" / "company_names.json"
EXCLUDED_COMPANY_NAMES_FILE = BASE_DIR / "config" / "company_names_excluded.json"
COMPANY_LIST_SPREADSHEET_NAME = "company_list_spreadsheet"
COMPANY_LIST_SPREADSHEET_NAME_FALLBACKS = ["company_list"]
COMPANY_LIST_WORKSHEET_NAME = "Company_List"
COMPANY_LIST_FOLDER_NAME = "webscraping"
COMPANY_NAME_START_ROW = 4

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"


class GoogleSheetAccessError(RuntimeError):
    """Raised when Google Sheets or Drive blocks a company_list operation."""


def credential_headers(credentials):
    if not credentials.valid or credentials.expired:
        credentials.refresh(Request())
    return {"Authorization": f"Bearer {credentials.token}"}


def raise_google_error(response, action):
    if response.ok:
        return

    try:
        detail = response.json()
    except ValueError:
        detail = response.text

    raise GoogleSheetAccessError(
        f"Google API could not {action}: "
        f"{response.status_code} {response.reason}; {detail}"
    )


def find_company_list_sheet(credentials):
    headers = credential_headers(credentials)
    files = []
    for spreadsheet_name in [
        COMPANY_LIST_SPREADSHEET_NAME,
        *COMPANY_LIST_SPREADSHEET_NAME_FALLBACKS,
    ]:
        query = (
            f"name = '{spreadsheet_name}' "
            f"and mimeType = '{SPREADSHEET_MIME_TYPE}' "
            "and trashed = false"
        )
        params = {
            "q": query,
            "fields": "files(id,name,parents,modifiedTime,webViewLink)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "pageSize": 10,
        }
        response = requests.get(
            DRIVE_FILES_URL,
            headers=headers,
            params=params,
            timeout=30,
        )
        raise_google_error(response, "find company_list spreadsheet")
        files = response.json().get("files", [])
        if files:
            break

    if not files:
        raise FileNotFoundError(
            "Could not find a Google Sheet named "
            f"{COMPANY_LIST_SPREADSHEET_NAME!r}."
        )

    folder_query = (
        f"name = '{COMPANY_LIST_FOLDER_NAME}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    folder_response = requests.get(
        DRIVE_FILES_URL,
        headers=headers,
        params={
            "q": folder_query,
            "fields": "files(id,name)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "pageSize": 10,
        },
        timeout=30,
    )
    raise_google_error(folder_response, "find webscraping Drive folder")
    folder_ids = {
        folder["id"]
        for folder in folder_response.json().get("files", [])
    }
    if folder_ids:
        for file_metadata in files:
            if folder_ids & set(file_metadata.get("parents", [])):
                return file_metadata

    return files[0]


def open_company_list_worksheet(credentials, spreadsheet_id):
    spreadsheet = gspread.authorize(credentials).open_by_key(spreadsheet_id)
    try:
        return spreadsheet.worksheet(COMPANY_LIST_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.sheet1


def parse_company_list_values(values):
    names = []
    for row in values[COMPANY_NAME_START_ROW - 1:]:
        value = str(row[0] if row else "").strip()
        if value:
            names.append(value)
    return names


def parse_updated_at(values):
    if not values:
        return None

    value = str(values[0][0] if values[0] else "").strip()
    if not value.lower().startswith("updated:"):
        return None

    raw_timestamp = value.split(":", 1)[1].strip()
    for timestamp_format in ["%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC"]:
        try:
            parsed = datetime.strptime(raw_timestamp, timestamp_format)
            return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return None


def read_company_list_sheet(credentials):
    file_metadata = find_company_list_sheet(credentials)
    worksheet = open_company_list_worksheet(credentials, file_metadata["id"])
    values = worksheet.get("A:A")
    updated_at = parse_updated_at(values)
    if updated_at:
        file_metadata["modifiedTime"] = updated_at
    return parse_company_list_values(values), file_metadata


def write_company_list_sheet(credentials, names):
    file_metadata = find_company_list_sheet(credentials)
    worksheet = open_company_list_worksheet(credentials, file_metadata["id"])
    existing_values = worksheet.get("A:A")
    existing_names = parse_company_list_values(existing_values)
    existing_keys = {name.casefold() for name in existing_names}
    new_names = sorted(
        [
            name
            for name in names
            if str(name).strip() and str(name).strip().casefold() not in existing_keys
        ],
        key=str.casefold,
    )
    updated_at = datetime.now(timezone.utc).strftime("Updated: %Y-%m-%d %H:%M UTC")

    worksheet.update([[updated_at]], range_name="A1")
    if new_names:
        start_row = max(COMPANY_NAME_START_ROW, len(existing_values) + 1)
        end_row = start_row + len(new_names) - 1
        worksheet.update(
            [[name] for name in new_names],
            range_name=f"A{start_row}:A{end_row}",
        )
    return file_metadata


def read_local_company_list():
    if not COMPANY_NAMES_FILE.exists():
        return []
    with open(COMPANY_NAMES_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)
    if isinstance(data, dict):
        names = []
        for value in data.values():
            if isinstance(value, list):
                names.extend(value)
            else:
                names.append(value)
        return names
    if isinstance(data, list):
        return data
    return []


def write_local_company_list(names):
    COMPANY_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COMPANY_NAMES_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(names, key=str.casefold), file, indent=2, ensure_ascii=False)
        file.write("\n")


def read_excluded_company_list():
    if not EXCLUDED_COMPANY_NAMES_FILE.exists():
        return []
    with open(EXCLUDED_COMPANY_NAMES_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)
    if isinstance(data, list):
        return data
    return []


def write_excluded_company_list(names):
    EXCLUDED_COMPANY_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EXCLUDED_COMPANY_NAMES_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(names, key=str.casefold), file, indent=2, ensure_ascii=False)
        file.write("\n")


# Backward-compatible names for existing callers during the Doc-to-Sheet transition.
read_company_list_doc = read_company_list_sheet
write_company_list_doc = write_company_list_sheet
