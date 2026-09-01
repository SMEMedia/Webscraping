# SME Dashboard Data Refresh

This repository provides a simple web page for refreshing data used by the Webscraping and Company Mentions dashboards. Day-to-day users do **not** need Python, a command prompt, or a copy of this repository.

## For dashboard operators

1. Open the refresh-page link supplied by the administrator.
2. Leave **Collect details for new articles** selected for a normal refresh.
3. Select **Run dashboard refresh**.
4. Keep the page open until it says **Refresh complete**.
5. The dashboards will read the refreshed worksheets from `AM_Enriched_Articles`.
   The ZIP download is an optional backup for an administrator.

If a run stops, expand **Technical details**, copy the message, and send it to the dashboard administrator. Never send or upload a Google service-account key.

## One-time administrator setup

The Google service account must have access to GA4 property `432233519`, the `AM_Enriched_Articles` and `company_list_spreadsheet` Google Sheets, and the Drive folder named `webscraping`.

1. Sign in to [Streamlit Community Cloud](https://share.streamlit.io/).
2. Create an app from this repository and choose `app.py` as the main file.
3. Open **Settings → Secrets** and paste the service-account values using this structure:

```toml
[google_service_account]
type = "service_account"
project_id = "your-project-id"
private_key_id = "your-private-key-id"
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "service-account@project.iam.gserviceaccount.com"
client_id = "your-client-id"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "your-certificate-url"
universe_domain = "googleapis.com"
```

Use values from the existing service-account JSON. Never add that file to GitHub or paste it anywhere except Streamlit's encrypted Secrets screen.

## How dashboard publishing works

Each successful run replaces the relevant output worksheets inside the
`AM_Enriched_Articles` Google Sheet. The dashboards already use that Sheet, so
operators do not need to move files after a refresh. The app also creates local CSV
copies during the run and offers them as an optional ZIP backup.

## Repository contents

- `app.py`: operator-friendly page
- `scripts/`: scraping and Google Analytics code
- `config/`: non-secret keyword and company lists
- `data/cache/`: temporary article details created during a run (not published to GitHub)
- `requirements.txt`: software installed automatically by Streamlit

## Maintenance

- Never commit credentials, tokens, `secrets.toml`, or service-account JSON files.
- The default run covers the previous 700 days through today.
- Local testing is optional: install the requirements and run `streamlit run app.py`.
