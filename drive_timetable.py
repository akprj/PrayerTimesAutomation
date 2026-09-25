import io
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


FOLDER_ID = "1LzalNF3Qa-ey1V-pkwRiFBV_1g2AXn08"


def fetch_latest_timetable():
    """Fetch the newest image directly inside the folder into memory."""
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    if not credentials_path:
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set."
        )

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )

    drive = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    try:
        response = drive.files().list(
            q=(
                f"'{FOLDER_ID}' in parents "
                "and trashed = false "
                "and mimeType contains 'image/'"
            ),
            orderBy="createdTime desc",
            pageSize=1,
            fields="files(id,name,createdTime,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        files = response.get("files", [])

        if not files:
            raise RuntimeError(
                "No images found. Check the folder ID, its contents, "
                "and the service account's Viewer access."
            )


        latest = next(iter(files))
        stream = io.BytesIO()


        try:
            request = drive.files().get_media(
                fileId=latest["id"],
                supportsAllDrives=True,
            )

            downloader = MediaIoBaseDownload(stream, request)
            done = False

            while not done:
                _, done = downloader.next_chunk()

            stream.seek(0)
            return stream, latest

        except Exception:
            stream.close()
            raise

    finally:
        drive.close()
