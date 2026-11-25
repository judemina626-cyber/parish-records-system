# San Julian de Cuenca Parish Church — Digital Parish Services

## Optimized update
- Reworked the dashboard into a premium royal-navy and champagne-gold layout.
- Improved contrast and font readability, including mobile layouts.
- Dashboard statistics, recent records, and analytics are queried from the existing database.
- Analytics filters: 7D, 30D, 3M, and 1Y. Empty data shows an honest empty state.
- Removed the hard-coded sample-style parish announcement; the panel now shows an empty state because this project version has no announcements table/module.
- Replaced the large embedded PNG background with a smaller WebP data image to reduce page weight.
- Added non-destructive database indexes for common dashboard queries.
- Limited dashboard queries to the latest six records and aggregated counts instead of loading entire activity/record tables.
- Reduced notification polling from every 3 seconds to every 15 seconds and session checks from every 15 seconds to every 60 seconds.
- Existing login, permissions, certificate requests, OCR, reports, record management, and database schema are retained.

## Important data-safety notes
- This update does not drop tables, delete records, or reset passwords.
- Keep your existing database and Render environment variables.
- Do not include `.env`, database files, or backups when sharing/deploying the source ZIP.
- The `.env.example` file contains placeholders only.
- A real runtime test against your live database is still required before deploying.

## Run locally (Windows PowerShell)
1. Extract the ZIP and open the folder in VS Code.
2. Create and activate a virtual environment:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```
3. Install packages:
   ```powershell
   python -m pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env`, then set your own `FLASK_SECRET_KEY`. Keep your existing `DATABASE_URL` if you use PostgreSQL.
5. Start:
   ```powershell
   python app.py
   ```
6. Open `http://127.0.0.1:5000`.

## Render deployment
- Do not create a new Render database.
- Keep the existing `DATABASE_URL` and `FLASK_SECRET_KEY` environment variables configured in Render.
- Deploy only after testing locally and confirming that the expected records and accounts are present.
- Never upload or share `.env` or database credentials.

## Android / phone
This source update does not include a compiled APK. The hosted site still requires internet access. First verify the updated website on laptop and phone, then build or install the Android wrapper.
