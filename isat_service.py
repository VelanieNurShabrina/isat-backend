from flask import Flask, jsonify, request
from flask_cors import CORS
import serial, time, re, sqlite3, threading
import os

# === CONFIG ===
PORT = '/dev/ttyACM0'      # sesuaikan port modem
BAUDRATE = 115200
DEFAULT_INTERVAL = 10      # detik (default)
DB_PATH = 'isat_data.db'
FLASK_PORT = 5000

app = Flask(__name__)
CORS(app)

# === SERIAL INIT ===
ser = None
current_interval = DEFAULT_INTERVAL   # nilai interval aktif
serial_lock = threading.Lock()        # biar aman antara polling dan call

def open_serial():
    """Membuka koneksi serial ke modem."""
    global ser
    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=1)
        print(f"[OK] Serial opened at {PORT}")
    except Exception as e:
        print("[ERROR] Serial open failed:", e)
        ser = None

open_serial()

# === DBM LOOKUP TABLE (mapping dari Pak Eko) ===
DBM_LOOKUP = {
    0: 0,
    1: -133, 2: -132.5, 3: -132, 4: -131.5, 5: -131, 6: -130.5,
    7: -130, 8: -129.5, 9: -129, 10: -128.5, 11: -128, 12: -127.5,
    13: -127, 14: -126.5, 15: -126, 16: -125.5, 17: -125, 18: -124.5,
    19: -124, 20: -123.5, 21: -123, 22: -122.5, 23: -122, 24: -121.5,
    25: -121, 26: -120.5, 27: -120, 28: -119.5, 29: -119, 30: -118.5,
    31: -118, 32: -117.5, 33: -117, 34: -116.5, 35: -116, 36: -115.5,
    37: -115, 38: -114.5, 39: -114, 40: -113.5, 41: -113, 42: -112.5,
    43: -112, 44: -111.5, 45: -111, 46: -110.5, 47: -110, 48: -109.5,
    49: -109, 50: -108.5, 51: -108, 52: -107.5, 53: -107, 54: -106.5,
    55: -106
}

# === PARSE AT+CSQ ===
def parse_csq_response(resp):
    """Parse respons AT+CSQ dan ubah ke RSSI, dBm, dan BER."""
    m = re.search(r'\+CSQ:\s*(\d+),(\d+)', resp)
    if m:
        rssi = int(m.group(1))
        ber = int(m.group(2))
        dbm = DBM_LOOKUP.get(rssi, None)
        return rssi, dbm, ber
    return None, None, None

# === READ SIGNAL ONCE ===
def read_csq_once():
    """Kirim perintah AT+CSQ dan ambil hasilnya."""
    global ser
    if ser is None:
        open_serial()
        if ser is None:
            return None, None, None
    try:
        with serial_lock:
            ser.reset_input_buffer()
            ser.write(b'AT+CSQ\r')
            time.sleep(0.5)
            resp = ser.read_all().decode(errors='ignore')
        return parse_csq_response(resp)
    except Exception as e:
        print("[ERROR] Serial read failed:", e)
        ser = None
        return None, None, None

# === DATABASE HELPERS ===
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
    Ambil data sinyal (timestamp, rssi, dbm, ber).
    Jika start/end (unix seconds) diberikan -> filter dalam window.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if start is not None and end is not None:
        sql = "SELECT timestamp, rssi, dbm, ber FROM csq_log WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp DESC LIMIT ?"
        cur.execute(sql, (start, end, limit))
    elif start is not None:
        sql = "SELECT timestamp, rssi, dbm, ber FROM csq_log WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?"
        cur.execute(sql, (start, limit))
    elif end is not None:
        sql = "SELECT timestamp, rssi, dbm, ber FROM csq_log WHERE timestamp < ? ORDER BY timestamp DESC LIMIT ?"
        cur.execute(sql, (end, limit))
    else:
        sql = "SELECT timestamp, rssi, dbm, ber FROM csq_log ORDER BY timestamp DESC LIMIT ?"
        cur.execute(sql, (limit,))

    rows = cur.fetchall()
    conn.close()
    rows.reverse()  # supaya urut dari yang lama -> baru
    return [{"timestamp": r[0], "rssi": r[1], "dbm": r[2], "ber": r[3]} for r in rows]

# === FUNGSI CALL ===
def make_call(number="+870772001899", call_seconds=15):
    """Melakukan panggilan otomatis ke nomor tertentu, lalu hangup setelah call_seconds detik."""
    global ser
    if ser is None:
        open_serial()
        if ser is None:
            return {"status": "error", "msg": "Serial not open"}

    try:
        with serial_lock:
            ser.reset_input_buffer()
            cmd = f"ATD{number};\r".encode()
            ser.write(cmd)
            time.sleep(0.5)
            resp_start = ser.read_all().decode(errors='ignore')

            time.sleep(call_seconds)

            ser.write(b"ATH\r")
            time.sleep(0.5)
            resp_end = ser.read_all().decode(errors='ignore')

        # rapihin output
        def clean_resp(s):
            return s.replace("\r", "").replace("\n\n", "\n").strip()

        print(f"[CALL] Dialing {number} →\n{clean_resp(resp_start)}")
        print(f"[CALL] Hangup →\n{clean_resp(resp_end)}")

        return {
            "status": "ok",
            "number": number,
            "call_seconds": call_seconds,
            "resp_start": clean_resp(resp_start),
            "resp_end": clean_resp(resp_end)
        }

    except Exception as e:
        print("[ERROR] make_call failed:", e)
        ser = None
        return {"status": "error", "msg": str(e)}

# === AUTO-CALL SECTION ===
auto_call_enabled = False
auto_call_interval = 1800   # default 30 menit
auto_call_duration = 15     # durasi panggilan (detik)
auto_call_thread = None

def auto_call_loop():
    """Loop auto-call periodik."""
    global auto_call_enabled, auto_call_interval, auto_call_duration
    next_call = time.time()

    while True:
        if auto_call_enabled:
            print(f"[AUTO-CALL] Melakukan panggilan ke +870772001899 (interval={auto_call_interval}s)")
            res = make_call("+870772001899", auto_call_duration)
            print(f"[AUTO-CALL] Hasil: {res.get('status')} (durasi {auto_call_duration} detik)")

            next_call += auto_call_interval
            sleep_time = next_call - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_call = time.time()
        else:
            time.sleep(1)

@app.route('/auto_call/start', methods=['GET'])
def start_auto_call():
    """Aktifkan auto-call periodik dengan interval & durasi tertentu."""
    global auto_call_enabled, auto_call_thread, auto_call_interval, auto_call_duration
    try:
        interval = int(request.args.get('interval', 1800))
        duration = int(request.args.get('secs', 15))

        if interval < 10 or interval > 86400:
            return jsonify({"status": "error", "msg": "Interval harus antara 10–86400 detik"})
        if duration < 5 or duration > 300:
            return jsonify({"status": "error", "msg": "Durasi harus antara 5–300 detik"})

        auto_call_interval = interval
        auto_call_duration = duration
        auto_call_enabled = True

        if not auto_call_thread or not auto_call_thread.is_alive():
            auto_call_thread = threading.Thread(target=auto_call_loop, daemon=True)
            auto_call_thread.start()

        print(f"[AUTO-CALL] Dijalankan tiap {auto_call_interval}s, durasi {auto_call_duration}s")
        return jsonify({
            "status": "ok",
            "auto_call_enabled": True,
            "interval": auto_call_interval,
            "duration": auto_call_duration
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/auto_call/stop', methods=['GET'])
def stop_auto_call():
    """Matikan auto-call periodik."""
    global auto_call_enabled
    auto_call_enabled = False
    print("[AUTO-CALL] Dihentikan.")
    return jsonify({"status": "ok", "auto_call_enabled": False})

@app.route('/auto_call/status', methods=['GET'])
def status_auto_call():
    """Cek status auto-call."""
    return jsonify({
        "auto_call_enabled": auto_call_enabled,
        "interval": auto_call_interval,
        "duration": auto_call_duration
    })

auto_call_thread = threading.Thread(target=auto_call_loop, daemon=True)
auto_call_thread.start()

# === POLLING LOOP ===
def polling_loop():
    """Loop pembacaan sinyal (CSQ) periodik."""
    global current_interval
    next_poll = time.time()
    last_ts = None

    while True:
        ts = int(time.time())
        rssi, dbm, ber = read_csq_once()

        if rssi is not None:
            insert_csq(ts, rssi, dbm, ber)
            print(f"[LOG] {time.strftime('%H:%M:%S')} → RSSI={rssi}, dBm={dbm}, BER={ber}")
        else:
            print(f"[WARN] {time.strftime('%H:%M:%S')} → gagal baca sinyal")

        if last_ts:
            print(f"[DEBUG] Jeda antar polling: {ts - last_ts}s (interval={current_interval}s)")
        last_ts = ts

        next_poll += current_interval
        sleep_time = next_poll - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            next_poll = time.time()

poll_thread = threading.Thread(target=polling_loop, daemon=True)
poll_thread.start()

# === API ENDPOINTS ===
@app.route('/signal', methods=['GET'])
def signal_now():
    rssi, dbm, ber = read_csq_once()
    return jsonify({"rssi": rssi, "dbm": dbm, "ber": ber})

@app.route('/history', methods=['GET'])
def history():
    """
    GET /history?limit=200&start=UNIX&end=UNIX
    - start, end optional (unix seconds)
    - limit optional
    """
    limit = request.args.get('limit', default=300, type=int)
    start = request.args.get('start', default=None, type=int)
    end = request.args.get('end', default=None, type=int)

    data = get_history(limit=limit, start=start, end=end)
    return jsonify({"data": data})

@app.route('/config', methods=['GET'])
def update_config():
    global current_interval
    try:
        new_interval = float(request.args.get('interval', current_interval))
        if new_interval < 1 or new_interval > 300:
            return jsonify({"status": "error", "message": "Interval 1–300 detik"})
        current_interval = new_interval
        print(f"[CONFIG] Interval polling diubah ke {current_interval} detik")
        return jsonify({"status": "ok", "interval": current_interval})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/call', methods=['GET'])
def call_now():
    number = request.args.get('number', '+870772001899')
    secs = int(request.args.get('secs', 15))
    result = make_call(number=number, call_seconds=secs)
    return jsonify(result)

# === MAIN RUN ===
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"[START] Flask running on 0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)

