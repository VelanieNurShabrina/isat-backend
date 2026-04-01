# init_db.py
import sqlite3

db = sqlite3.connect('isat_data.db')
c = db.cursor()
c.execute('''
CREATE TABLE IF NOT EXISTS csq_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    rssi INTEGER,
    dbm REAL,
    ber INTEGER
)
''')
db.commit()
db.close()
print("DB initialized")
