# San Julian de Cuenca Parish Church — Digital Parish Services

## Local setup (Windows PowerShell)

1. Extract the ZIP to a folder.
2. Open that folder in VS Code.
3. Create a virtual environment:
   ```powershell
   python -m venv venv
   ```
4. Activate it:
   ```powershell
   .\venv\Scripts\Activate.ps1
   ```
   If PowerShell blocks activation, run this once in the same terminal:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\venv\Scripts\Activate.ps1
   ```
5. Install dependencies:
   ```powershell
   python -m pip install -r requirements.txt
   ```
6. Copy `.env.example` to `.env` and set a private `FLASK_SECRET_KEY` value.
7. Start the app:
   ```powershell
   python app.py
   ```
8. Open `http://127.0.0.1:5000`.

## Render deployment

The included Dockerfile installs Tesseract OCR and starts the Flask app with Gunicorn on port 10000. Configure `FLASK_SECRET_KEY` and `DATABASE_URL` as Render environment variables. Use a persistent PostgreSQL database for production parish records.

## Data safety

- No `.env`, database file, or backup containing parish records is included in this ZIP.
- The app's existing database schema and record-handling routes are retained; the visual update does not intentionally reset or delete records.
- Keep `.env` private. Do not upload it to GitHub or share it publicly.
