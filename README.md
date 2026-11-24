# San Julian de Cuenca Parish Church — Digital Parish Services

## Changes in this update
- Staff and administrators see the pending certificate request count in the sidebar.
- The signed-in system checks for new certificate requests every 3 seconds.
- When a new request arrives while a staff/admin page is open, an in-page alert appears with the applicant, certificate type, and request number.
- Existing Certificate Requests inbox and request-processing workflow are retained.
- Existing database tables and parish-record features are retained; no destructive database migration is added.

## Default account
- Administrator username: `admin`
- Initial password: `admin123` only if the existing database did not already have an `admin` account.
- There is no built-in Staff account/password. Sign in as Administrator, open **User Management**, and create a Staff account with a password you choose.
- If the existing admin password was changed previously, this update does not reset it.

## Run locally (Windows PowerShell)
1. Extract the ZIP and open the folder in VS Code.
2. Run:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and set a private `FLASK_SECRET_KEY`.
4. Start: `python app.py`
5. Open `http://127.0.0.1:5000`.

## Render deployment
- Keep the existing PostgreSQL database and Render environment variables.
- Update the existing Render web service with these project files; do not create another database.
- Confirm `DATABASE_URL` and `FLASK_SECRET_KEY` remain configured.
- Never upload `.env` or share database credentials.

## Android app project
The `android/` folder is an Android Studio WebView wrapper for:
`https://parish-records-system-3.onrender.com`

Open `android/` in Android Studio, wait for Gradle sync, then choose **Build > Build Bundle(s) / APK(s) > Build APK(s)**. Output: `android/app/build/outputs/apk/debug/app-debug.apk`.
The app requires internet because the parish system and PostgreSQL database are hosted online.
