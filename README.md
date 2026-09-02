# Web Scraping and Company Mentions Dashboard

This dashboard refreshes and displays article, audience, and company-mention information for SME Media. It is designed for routine use in a web browser.

## Important links

- [Open the dashboard](https://smewebscraping.streamlit.app/)
- [SMEMedia repository](https://github.com/SMEMedia/Webscraping)
- [Approved company list](https://docs.google.com/spreadsheets/d/194SdsfBVsJVKSOzV64jLB4ds3iE8hHVdf0RmwKYj_ag/edit)
- [Dashboard data](https://docs.google.com/spreadsheets/d/1JLfIsfecWiGYfIzsYfDfOcRvlritfCmSFhUuJPxj0W4/edit)

## Refresh the data

1. Update company names in column A of the **Company_List** tab in the approved company list, beginning on row 4.
2. Open the dashboard and select **Run Refresh**.
3. Leave **Collect details for new articles** selected for a normal refresh.
4. Select **Run dashboard refresh**.
5. Keep the page open until **Refresh complete** appears.
6. Open the **Dashboard** tab and select **Reload dashboard data**.

The refresh replaces the appropriate worksheets in the shared dashboard-data Sheet. The optional ZIP download is a backup and is not required for normal use.

## Use the dashboard

Choose a view from the dashboard menu. Available views cover company mentions, articles, overall performance, title keywords, section performance, and returning-user behavior. Use the date, company, author, and section filters when they appear.

## Troubleshooting

### The refresh appears to be stuck

- Keep the page open; a full refresh can take several minutes.
- Do not start a second refresh in another tab.
- If no progress appears after 15 minutes, capture a screenshot and expand **Technical details**.
- Send the message, approximate start time, and screenshot to the dashboard support contact.

### A company is missing

- Confirm the name is in column A of the **Company_List** tab, on row 4 or below.
- Remove leading or trailing spaces and use the company’s usual published spelling.
- Run a new refresh after changing the list.
- If the app says it used a backup list, ask the Google Workspace owner to verify access to the Sheet.

### New articles or metrics are missing

- Confirm the refresh finished successfully.
- Select **Reload dashboard data** after the refresh.
- Check that the selected date range includes the expected article.
- Very recent analytics can still be processing; check again the next business day.

### The dashboard opens but shows an error

- Refresh the browser once.
- Open [Streamlit Community Cloud](https://share.streamlit.io/) and confirm the app is running.
- If the app shows an access or credential error, contact the technical owner. Never paste credentials into the dashboard, GitHub, email, or chat.

## Ongoing maintenance

- Update the approved company list before running a refresh.
- Review the completion message after every refresh.
- Keep dashboard and Google Sheet access assigned to at least two current SME employees.
- Escalate credential, permission, deployment, or code errors to the assigned technical owner.

