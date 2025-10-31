from flask import Flask, jsonify, request
from flask_cors import CORS
import serial, time, re, sqlite3, threading, os, requests

# === CONFIG ===
PORT = '/dev/ttyACM0'      # sesuaikan port modem di Raspberry
BAUDRATE = 115200
DEFAULT_INTERVAL = 10      # detik (default)
DB_PATH = 'isat_data.db'
FLASK_PORT = 5000
CLOUD_URL = "https://web-production-7408.up.railway.app/history"  # endpoint Railway

app = Flask(__name__)
CORS(app)

# === SERIAL INIT ===
ser = None
current_interval = DEFAULT_INTERVAL
serial_lock = threading.Lock()

def open_serial():
    """Membuka koneksi serial ke modem (hanya di Raspberry)."""
    global ser
    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=1)
        print(f"[OK] Serial opened at {PORT}")
    except Exception as e:
        print("[ERROR] Serial open failed:", e)
        ser = None


# === DBM LOOKUP TABLE ===
DBM_LOOKUP = {i: -133 + 0.5 * (i - 1) for i in range(1, 56)}
DBM_LOOKUP[0] = 0


# === DATABASE FUNCTIONS ===
def init_db():
    """Buat tabel csq_log kalau belum ada."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS csq_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                rssi INTEGER,
                dbm REAL,
                ber INTEGER
            )
        """)
        conn.commit()
        conn.close()
        print("[INIT] Database siap digunakan ✅", flush=True)
    except Exception as e:
        print("[ERROR] Gagal inisialisasi database:", e, flush=True)


def insert_csq(timestamp, rssi, dbm, ber):
    """Masukkan data sinyal ke database."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO csq_log (timestamp, rssi, dbm, ber) VALUES (?, ?, ?, ?)",
            (timestamp, rssi, dbm, ber)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[ERROR] DB insert:", e)


def get_history(limit=300, start=None, end=None):
    """
    Ambil data sinyal dari database (lokal/cloud).
    Jika start dan end diberikan → filter berdasarkan rentang waktu.
    """
    if not os.path.exists(DB_PATH):
        print("[WARN] Database tidak ditemukan di cloud.")
        return []

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        if start and end:
            sql = """SELECT timestamp, rssi, dbm, ber 
                     FROM csq_log 
                     WHERE timestamp BETWEEN ? AND ? 
                     ORDER BY timestamp ASC 
                     LIMIT ?"""
            cur.execute(sql, (start, end, limit))
        else:
            sql = """SELECT timestamp, rssi, dbm, ber 
                     FROM csq_log 
                     ORDER BY timestamp ASC 
                     LIMIT ?"""
            cur.execute(sql, (limit,))

        rows = cur.fetchall()
        return [{"timestamp": r[0], "rssi": r[1], "dbm": r[2], "ber": r[3]} for r in rows]

    except Exception as e:
        print("[ERROR] Query gagal:", e)
        return []
    finally:
        conn.close()


# === SYNC KE CLOUD ===
def send_to_cloud(timestamp, rssi, dbm, ber):
    """Kirim data ke Railway cloud (POST /history)."""
    payload = {"timestamp": timestamp, "rssi": rssi, "dbm": dbm, "ber": ber}
    try:
        res = requests.post(CLOUD_URL, json=payload, timeout=5)
        if res.status_code == 200:
            print("[SYNC] Data terkirim ke cloud ✅", flush=True)
        else:
            print("[SYNC] Gagal kirim ke cloud ❌", res.status_code, flush=True)
    except Exception as e:
        print("[SYNC ERROR]", e, flush=True)


# === POLLING LOOP (untuk Raspberry, tidak aktif di Railway) ===
def polling_loop():
    global current_interval
    next_poll = time.time()
    last_ts = None

    while True:
        ts = int(time.time())
        rssi, dbm, ber = 17, -125, 0  # dummy contoh (karena cloud tidak punya modem)
        insert_csq(ts, rssi, dbm, ber)
        print(f"[LOG] {time.strftime('%H:%M:%S')} → RSSI={rssi}, dBm={dbm}, BER={ber}")
        send_to_cloud(ts, rssi, dbm, ber)

        if last_ts:
            print(f"[DEBUG] Jeda antar polling: {ts - last_ts}s (interval={current_interval}s)")
        last_ts = ts

        next_poll += current_interval
        sleep_time = next_poll - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            next_poll = time.time()


# === API ENDPOINTS ===
@app.route('/history', methods=['GET'])
def history():
    """Ambil data history dengan optional filter start-end."""
    limit = request.args.get('limit', default=300, type=int)
    start = request.args.get('start', type=int)
    end = request.args.get('end', type=int)

    data = get_history(limit=limit, start=start, end=end)
    return jsonify({"data": data})


@app.route('/history', methods=['POST'])
def add_history():
    """Terima data dari Raspberry dan simpan ke DB di cloud."""
    try:
        data = request.get_json()
        print("[CLOUD] Data diterima:", data, flush=True)

        timestamp = data.get("timestamp")
        rssi = data.get("rssi")
        dbm = data.get("dbm")
        ber = data.get("ber")

        if timestamp and rssi is not None:
            insert_csq(timestamp, rssi, dbm, ber)
            print(f"[DB] Data disimpan: RSSI={rssi}, dBm={dbm}, BER={ber}")
        else:
            print("[WARN] Data tidak lengkap, tidak disimpan.")

        return jsonify({"status": "ok", "msg": "Data diterima dan disimpan", "data": data})

    except Exception as e:
        print("[ERROR] Gagal menerima data:", e, flush=True)
        return jsonify({"status": "error", "msg": str(e)}), 500


@app.route('/')
def index():
    return jsonify({
        "status": "ok",
        "message": "ISAT Backend is running successfully 🚀 v3",
        "available_routes": [
            "/history (GET, POST)",
            "/config",
            "/config/interval"
        ]
    })


# === KONFIGURASI INTERVAL ===
@app.route('/config/interval', methods=['GET'])
def get_interval():
    global current_interval
    return jsonify({"status": "ok", "interval": current_interval})


@app.route('/config', methods=['GET'])
def set_interval():
    global current_interval
    try:
        new_interval = request.args.get('interval', type=int)
        if new_interval and new_interval > 0:
            current_interval = new_interval
            print(f"[CONFIG] Interval diubah jadi {new_interval} detik", flush=True)
            return jsonify({"status": "ok", "interval": current_interval})
        else:
            return jsonify({"status": "error", "msg": "Invalid interval value"}), 400
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


# === MAIN RUN ===
if __name__ == '__main__':
    init_db()

    on_cloud = os.environ.get("RAILWAY_ENVIRONMENT") is not None

    if on_cloud:
        print("[INFO] Running on Railway Cloud - serial & polling dinonaktifkan.", flush=True)
        ser = None
    else:
        print("[INFO] Running locally - membuka koneksi serial.", flush=True)
        open_serial()
        poll_thread = threading.Thread(target=polling_loop, daemon=True)
        poll_thread.start()

    port = int(os.environ.get("PORT", 5000))
    print(f"[START] Flask running on 0.0.0.0:{port}", flush=True)
    app.run(host='0.0.0.0', port=port)
