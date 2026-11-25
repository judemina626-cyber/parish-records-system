import sqlite3

DB_PATH = "database/parish_records.db"

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n===== DATABASE TABLES =====")
    tables = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()

    for table in tables:
        print(table[0])

    print("\n===== RECORD COUNT =====")
    count = cursor.execute(
        "SELECT COUNT(*) FROM records"
    ).fetchone()

    print("Total Records:", count[0])

    print("\n===== SAMPLE RECORDS =====")
    rows = cursor.execute(
        "SELECT * FROM records LIMIT 10"
    ).fetchall()

    for row in rows:
        print(row)

    conn.close()

except Exception as e:
    print("\nDATABASE ERROR:")
    print(e)