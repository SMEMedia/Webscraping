import json
import os
from pathlib import Path

from google.oauth2 import service_account


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "scripts" else SCRIPT_DIR
DEFAULT_SERVICE_ACCOUNT_FILE = (
    BASE_DIR / "config" / "stable-hologram-497015-i9-45282bfa717e.json"
)


def get_credentials(scopes):
    credentials_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if credentials_json:
        return service_account.Credentials.from_service_account_info(
            json.loads(credentials_json), scopes=scopes
        )

    credentials_path = Path(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or DEFAULT_SERVICE_ACCOUNT_FILE
    )

    if not credentials_path.exists():
        raise FileNotFoundError(
            "Google service account file not found: "
            f"{credentials_path}. Set GOOGLE_SERVICE_ACCOUNT_FILE to override."
        )

    return service_account.Credentials.from_service_account_file(
        credentials_path,
        scopes=scopes,
    )
