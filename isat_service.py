# isat_service.py — FIXED VERSION
from flask import Flask, jsonify, request
import serial, time, re, sqlite3, threading, os, json
from flask_cors import CORS
from datetime import datetime, timedelta, timezone
from pdu_encoder import encode_pdu
import requests
from isat_pdu_encoder import encode_isatphone_pdu
from queue import Queue


# ==================================================
# CONFIG
# ==================================================
PORT = os.environ.get(
    "ISAT_PORT",
    "/dev/serial/by-id/usb-Inmarsat_IsatPhone_2_DUMMY-if01"
)
BAUDRATE = 115200
CONFIG_FILE = "config.json"
DEFAULT_INTERVAL = 10
DB_PATH = os.environ.get("ISAT_DB", "isat_data.db")
FLASK_PORT = int(os.environ.get("ISAT_FLASK_PORT", 8000))
SMSC_NUMBER = "+870772001799"
sms_sending = False
sms_session_active = False
WIB = timezone(timedelta(hours=7))

# Mode flag: choose RSSI table (idle / dedicated)
call_active = False   # False = Idle mode, True = Dedicated mode
call_state = "idle"
call_start_time = None
call_max_duration = None
call_stop_by_user = False

# =========================
# AUTO MODE STATE
# =========================
auto_call_enabled = False
auto_sms_enabled = False

auto_call_interval = 60   # detik
auto_sms_interval = 300   # detik

auto_call_number = "+870772001899"
auto_sms_number = "+628xxxxxxxxx"
auto_sms_message = "Auto SMS Test"

last_auto_call_time = 0
auto_call_duration = 15   # detik
last_auto_sms_time = 0

# =========================
# TASK QUEUE (FIFO)
# =========================
task_queue = Queue()

# =========================
# CURRENT TASK STATE (UNTUK DASHBOARD)
# =========================
current_task = {
    "type": None,     # "CALL" | "SMS"
    "source": None    # "manual" | "auto"
}

# =========================
# LAST MANUAL CALL STATE
# =========================
last_manual_call = {
    "number": "",
    "duration": 0
}

active_call = {
    "number": None,
    "duration": None
}


# ==================================================
# INIT FLASK
# ==================================================
app = Flask(__name__)
CORS(app, supports_credentials=True)

# ==================================================
# UTIL
# ==================================================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_unix_wib() -> int:
    return int(datetime.now(WIB).timestamp())

def start_of_today_wib():
    now = datetime.now(WIB)
    start = datetime(now.year, now.month, now.day, tzinfo=WIB)
    return int(start.timestamp())


def enqueue_task(task: dict):
    """
    task = {
        "type": "CALL" | "SMS",
        "source": "manual" | "auto",
        "number": str,
        "duration": int (CALL),
        "message": str (SMS)
    }
    """
    task_queue.put(task)
    log(f"[QUEUE] Enqueued {task['type']} ({task['source']})")

# =========================
# CONFIG SAVE/LOAD
# =========================

def save_config():
    data = {
        "auto_sms": {
            "enabled": auto_sms_enabled,
            "interval": auto_sms_interval,
            "number": auto_sms_number,
            "message": auto_sms_message
        },
        "auto_call": {
            "enabled": auto_call_enabled,
            "interval": auto_call_interval,
            "number": auto_call_number,
            "duration": auto_call_duration
        }
    }

    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f)


def load_config():
    global auto_sms_enabled, auto_sms_interval
    global auto_sms_number, auto_sms_message
    global auto_call_enabled, auto_call_interval
    global auto_call_number, auto_call_duration

    if not os.path.exists(CONFIG_FILE):
        return

    with open(CONFIG_FILE) as f:
        data = json.load(f)

    auto_sms = data.get("auto_sms", {})
    auto_call = data.get("auto_call", {})

    auto_sms_enabled = auto_sms.get("enabled", False)
    auto_sms_interval = auto_sms.get("interval", 300)
    auto_sms_number = auto_sms.get("number", "")
    auto_sms_message = auto_sms.get("message", "")

    auto_call_enabled = auto_call.get("enabled", False)
    auto_call_interval = auto_call.get("interval", 60)
    auto_call_number = auto_call.get("number", "")
    auto_call_duration = auto_call.get("duration", 15)

    log("[CONFIG] Loaded from file")


# ==================================================
# SERIAL INIT
# ==================================================
ser = None
serial_lock = threading.Lock()
current_interval = DEFAULT_INTERVAL


def open_serial():
    """Buka port serial ke IsatPhone, simpan di global ser."""
    global ser
    try:
        ser = serial.Serial(PORT, BAUDRATE, timeout=1)
        log(f"[SERIAL] Dibuka pada {PORT}")
    except Exception as e:
        log(f"[WARN] Gagal membuka serial: {e}")
        ser = None


open_serial()

# ==================================================
# DATABASE INIT
# ==================================================
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cur = conn.cursor()

        # ================= CSQ LOG =================
        cur.execute("""
            CREATE TABLE IF NOT EXISTS csq_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                rssi INTEGER,
                dbm REAL,
                ber INTEGER
            )
        """)

        # ================= CALL LOGS =================
        cur.execute("""
            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                source TEXT,
                number TEXT,
                status TEXT,
                cause_code INTEGER,
                cause_desc TEXT
            )
        """)

        # ================= SMS LOGS =================
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sms_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                source TEXT,
                number TEXT,
                status TEXT
            )
        """)

        conn.commit()
        conn.close()

        log("[INIT] Database ready (CSQ + CALL + SMS + CAUSE SUPPORT)")

    except Exception as e:
        log(f"[ERROR] Init DB: {e}")


init_db()
load_config()

# =========================
# AUTO CLEANUP OLD DATA
# =========================
def cleanup_old_data():
    while True:
        try:
            cutoff = int(time.time()) - (7 * 24 * 3600)

            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            # Hapus CSQ lama
            cur.execute("DELETE FROM csq_log WHERE timestamp < ?", (cutoff,))

            # Hapus call log lama
            cur.execute("DELETE FROM call_logs WHERE timestamp < ?", (cutoff,))

            # Hapus sms log lama
            cur.execute("DELETE FROM sms_logs WHERE timestamp < ?", (cutoff,))

            conn.commit()
            conn.close()

            log("[CLEANUP] Old data (>7 days) deleted")

        except Exception as e:
            log(f"[CLEANUP ERROR] {e}")

        # jalan 1x per hari
        time.sleep(86400)


# ==================================================
# RSSI MAPPING TABLES (GMR2P)
# ==================================================

# Idle mode mapping 0–55
RSSI_IDLE_TABLE = {
    0:  (-999,   -133.0),
    1:  (-133.0, -132.5),
    2:  (-132.5, -132.0),
    3:  (-132.0, -131.5),
    4:  (-131.5, -131.0),
    5:  (-131.0, -130.5),
    6:  (-130.5, -130.0),
    7:  (-130.0, -129.5),
    8:  (-129.5, -129.0),
    9:  (-129.0, -128.5),
    10: (-128.5, -128.0),
    11: (-128.0, -127.5),
    12: (-127.5, -127.0),
    13: (-127.0, -126.5),
    14: (-126.5, -126.0),
    15: (-126.0, -125.5),
    16: (-125.5, -125.0),
    17: (-125.0, -124.5),
    18: (-124.5, -124.0),
    19: (-124.0, -123.5),
    20: (-123.5, -123.0),
    21: (-123.0, -122.5),
    22: (-122.5, -122.0),
    23: (-122.0, -121.5),
    24: (-121.5, -121.0),
    25: (-121.0, -120.5),
    26: (-120.5, -120.0),
    27: (-120.0, -119.5),
    28: (-119.5, -119.0),
    29: (-119.0, -118.5),
    30: (-118.5, -118.0),
    31: (-118.0, -117.5),
    32: (-117.5, -117.0),
    33: (-117.0, -116.5),
    34: (-116.5, -116.0),
    35: (-116.0, -115.5),
    36: (-115.5, -115.0),
    37: (-115.0, -114.5),
    38: (-114.5, -114.0),
    39: (-114.0, -113.5),
    40: (-113.5, -113.0),
    41: (-113.0, -112.5),
    42: (-112.5, -112.0),
    43: (-112.0, -111.5),
    44: (-111.5, -111.0),
    45: (-111.0, -110.5),
    46: (-110.5, -110.0),
    47: (-110.0, -109.5),
    48: (-109.5, -109.0),
    49: (-109.0, -108.5),
    50: (-108.5, -108.0),
    51: (-108.0, -107.5),
    52: (-107.5, -107.0),
    53: (-107.0, -106.5),
    54: (-106.5, -106.0),
    55: (-106.0, -999),
}

# Dedicated mode mapping 0–31
RSSI_DEDICATED_TABLE = {
    0:  (-121.5, -121.0),
    1:  (-121.0, -120.5),
    2:  (-120.5, -120.0),
    3:  (-120.0, -119.5),
    4:  (-119.5, -119.0),
    5:  (-119.0, -118.5),
    6:  (-118.5, -118.0),
    7:  (-118.0, -117.5),
    8:  (-117.5, -117.0),
    9:  (-117.0, -116.5),
    10: (-116.5, -116.0),
    11: (-116.0, -115.5),
    12: (-115.5, -115.0),
    13: (-115.0, -114.5),
    14: (-114.5, -114.0),
    15: (-114.0, -113.5),
    16: (-113.5, -113.0),
    17: (-113.0, -112.5),
    18: (-112.5, -112.0),
    19: (-112.0, -111.5),
    20: (-111.5, -111.0),
    21: (-111.0, -110.5),
    22: (-110.5, -110.0),
    23: (-110.0, -109.5),
    24: (-109.5, -109.0),
    25: (-109.0, -108.5),
    26: (-108.5, -108.0),
    27: (-108.0, -107.5),
    28: (-107.5, -107.0),
    29: (-107.0, -106.5),
    30: (-106.5, -106.0),
    31: (-106.0, -999),
}

# ==================================================
# DBM CALCULATOR
# ==================================================
def calculate_dbm_from_table(rssi: int, dedicated: bool = False):
    """Hitung dBm berdasarkan RSSI + mode (idle/dedicated) pakai tabel GMR2P."""
    table = RSSI_DEDICATED_TABLE if dedicated else RSSI_IDLE_TABLE

    if rssi not in table:
        return None

    low, high = table[rssi]
    if high == -999:
        # range open-ended (>= low)
        return low

    # ambil titik tengah range
    return (low + high) / 2.0


# ==================================================
# PARSE AT+CSQ (FINAL - DEDICATED FIX)
# ==================================================
def parse_csq_response(resp: str):
    global call_state

    if not resp:
        return None, None, None

    m = re.search(r"\+CSQ:\s*(\d+),(\d+)", resp)
    if not m:
        return None, None, None

    rssi = int(m.group(1))
    ber = int(m.group(2))

    # 🔥 Dedicated mode kalau sedang call
    dedicated = call_state in ["dialing", "waiting", "active"]

    log(f"[CSQ DEBUG] call_state={call_state} dedicated={dedicated}")

    dbm = calculate_dbm_from_table(rssi, dedicated)

    return rssi, dbm, ber


# ==================================================
# READ CSQ
# ==================================================
def read_csq_once():
    global ser

    if ser is None:
        open_serial()
        if ser is None:
            return None, None, None

    try:
        with serial_lock:
            ser.reset_input_buffer()
            ser.write(b"AT+CSQ\r")
            time.sleep(0.3)
            resp = ser.read_all().decode(errors="ignore")

        return parse_csq_response(resp)

    except Exception as e:
        log(f"[ERROR] CSQ failed: {e}")
        return None, None, None


# ==================================================
# DB INSERT
# ==================================================
def insert_csq(ts: int, rssi: int, dbm: float, ber: int):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO csq_log (timestamp, rssi, dbm, ber) VALUES (?, ?, ?, ?)",
            (ts, rssi, dbm, ber),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[ERROR] DB insert: {e}")


# ==================================================
# CALL LOGGING
# ==================================================
def log_call_db(source, number, status, cause_code=None, cause_desc=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO call_logs 
            (timestamp, source, number, status, cause_code, cause_desc)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            int(time.time()),
            source,
            number,
            status,
            cause_code,
            cause_desc
        ))

        conn.commit()
        conn.close()

        log(f"[DB] Call logged: {number} {status} cause={cause_code}")

    except Exception as e:
        log(f"[DB][ERROR] Call log failed: {e}")


# ==================================================
# SMS LOGGING
# ==================================================
def log_sms_db(source, number, status):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO sms_logs (timestamp, source, number, status)
            VALUES (?, ?, ?, ?)
        """, (
            int(time.time()),
            source,
            number,
            status
        ))

        conn.commit()
        conn.close()

        log(f"[DB] SMS logged: {number} {status}")

    except Exception as e:
        log(f"[DB][ERROR] SMS log failed: {e}")


# ==================================================
# HISTORY QUERY
# ==================================================
def get_history(limit: int = 300, start: int | None = None, end: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if start is not None and end is not None:
        sql = """
            SELECT timestamp, rssi, dbm, ber FROM csq_log
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC LIMIT ?
        """
        cur.execute(sql, (start, end, limit))
    else:
        sql = """
            SELECT timestamp, rssi, dbm, ber FROM csq_log
            ORDER BY timestamp ASC LIMIT ?
        """
        cur.execute(sql, (limit,))

    rows = cur.fetchall()
    conn.close()

    return [
        {"timestamp": r[0], "rssi": r[1], "dbm": r[2], "ber": r[3]}
        for r in rows
    ]

CAUSE_MAP = {
    1: "Unassigned number",
    3: "No route to destination",
    6: "Channel unacceptable",
    8: "Operator barring",
    16: "Normal call clearing",
    17: "User busy",
    18: "No user responding",
    19: "No answer",
    21: "Call rejected",
    22: "Number changed",
    26: "Non selected user clearing",
    27: "Destination out of order",
    28: "Invalid number format",
    29: "Facility rejected",
    30: "Status enquiry response",
    31: "Normal unspecified",
    34: "No channel available",
    38: "Network out of order",
    41: "Temporary failure",
    42: "Switching congestion",
    47: "Resources unavailable",
}

# ==================================================
# MAKE CALL (BLOCKING VERSION)
# ==================================================
def make_call(number: str, call_seconds: int):
    global call_active, call_state, call_stop_by_user
    global active_call

    if not number:
        log("[CALL][ERROR] Number is empty")
        return

    number = number.strip()
    if not number.startswith("+"):
        number = "+" + number

    if call_seconds <= 0:
        call_seconds = 15
    if call_seconds > 300:
        call_seconds = 300

    # 🔥 SIMPAN ACTIVE CALL
    active_call["number"] = number
    active_call["duration"] = call_seconds

    # STATE AWAL
    call_active = True
    call_state = "dialing"
    call_stop_by_user = False

    log(f"[CALL] Dialing {number} ({call_seconds}s)")

    if ser is None:
        open_serial()
        if ser is None:
            call_state = "error"
            call_active = False
            return

    try:
        with serial_lock:
            ser.reset_input_buffer()
            ser.write(f"ATD{number};\r".encode())
            time.sleep(0.3)
    except Exception as e:
        log(f"[CALL][ERROR] {e}")
        call_state = "error"
        return

    call_monitor(call_seconds)



# ==================================================
# CALL MONITOR (FINAL STABLE VERSION)
# ==================================================
def call_monitor(timeout_sec: int):
    global call_active, call_state, call_stop_by_user
    global last_auto_call_time, active_call

    log("[CALL] Monitor started")

    start_time = time.time()
    call_state = "waiting"

    logged_number = active_call["number"]

    cause_code = None
    cause_desc = None

    connect_time = None
    duration = 0

    # 🔥 FLAG EVENT (INI KUNCI)
    has_dialing = False
    has_active = False

    clcc_map = {
        0: "ACTIVE",
        1: "HELD",
        2: "DIALING",
        3: "RINGING",
        4: "INCOMING",
        5: "WAITING"
    }

    while True:

        # =========================
        # STOP USER
        # =========================
        if call_stop_by_user:
            call_state = "stopped"
            log("[CALL] Stopped by user")
            break

        # =========================
        # TIMEOUT
        # =========================
        if time.time() - start_time >= timeout_sec:
            call_state = "timeout"
            log("[CALL] Timeout reached")
            break

        # =========================
        # QUERY MODEM
        # =========================
        with serial_lock:
            ser.write(b"AT+CLCC\r")
            time.sleep(0.5)
            resp = ser.read_all().decode(errors="ignore")

        log(f"[CLCC RAW]\n{resp}")

        # =========================
        # PARSE CLCC
        # =========================
        m = re.search(r"\+CLCC: \d+,\d+,(\d+),", resp)

        if m:
            stat = int(m.group(1))
            state_name = clcc_map.get(stat, "UNKNOWN")

            log(f"[CLCC STATE] stat={stat} ({state_name})")

            # 🔥 DIALING DETECT
            if stat == 2:
                has_dialing = True
                call_state = "dialing"

            # 🔥 ACTIVE DETECT (VALID ONLY IF BEFORE DIALING)
            elif stat == 0:
                if has_dialing:
                    has_active = True

                    if connect_time is None:
                        connect_time = time.time()
                        call_state = "active"
                        log("[CALL] CONNECT VALID (DIALING → ACTIVE)")
                else:
                    # 🚨 GHOST CONNECT (IGNORE)
                    log("[CALL] IGNORE GHOST ACTIVE (no dialing before)")

        # =========================
        # PARSE CAUSE (FIX INDEX)
        # =========================
        for line in resp.splitlines():
            if "+SKCCSI:" in line:
                try:
                    parts = line.split(",")

                    # 🔥 cause harus integer valid
                    if len(parts) > 5:
                        raw = parts[5].strip()

                        if raw.isdigit():
                            cause_code = int(raw)
                            cause_desc = CAUSE_MAP.get(cause_code, "Unknown")

                            log(f"[CAUSE DETECTED] {cause_code} ({cause_desc})")
                except:
                    pass

        # =========================
        # END DETECTION
        # =========================
        if "NO CARRIER" in resp:
            log("[CALL] NO CARRIER detected")
            break

        time.sleep(1)

    # =========================
    # HANGUP SAFETY
    # =========================
    with serial_lock:
        ser.write(b"ATH\r")
        time.sleep(1)
        ser.read_all()

    # =========================
    # FINAL STATUS (REALISTIC)
    # =========================
    final_status = "failed"

    if has_active and connect_time:
        duration = time.time() - connect_time
        log(f"[CALL] Connected duration: {duration:.2f}s")

        # 🔥 threshold realistis
        if duration >= 5:
            final_status = "success"

    # =========================
    # FALLBACK CAUSE (WAJAR BANGET)
    # =========================
    if cause_code is None:

        if final_status == "success":
            cause_code = 16
            cause_desc = "Normal call clearing"

        elif has_dialing:
            cause_code = 19
            cause_desc = "No answer"

        else:
            cause_code = 31
            cause_desc = "Normal unspecified"

        log(f"[CALL] Fallback cause used: {cause_code} ({cause_desc})")

    # =========================
    # SAVE DB
    # =========================
    log_call_db(
        current_task["source"],
        logged_number,
        final_status,
        cause_code,
        cause_desc
    )

    # =========================
    # RESET
    # =========================
    call_active = False
    call_stop_by_user = False
    call_state = "idle"

    active_call["number"] = None
    active_call["duration"] = None

    last_auto_call_time = time.time() + 10

    log(f"[CALL] Finished (status={final_status}, cause={cause_code})")

def auto_call_loop():
    global auto_call_enabled
    global auto_call_interval, auto_call_number, auto_call_duration
    global last_auto_call_time, call_active

    log("[AUTO CALL] Loop started")

    while True:
        time.sleep(1)

        # Auto call OFF → lewati
        if not auto_call_enabled:
            continue

        # Jangan enqueue kalau masih ada call aktif
        if call_active:
            continue

        now = time.time()

        # Belum waktunya trigger
        if now - last_auto_call_time < auto_call_interval:
            continue

        log("[AUTO CALL] Queueing auto call")

        # Update waktu trigger
        last_auto_call_time = now

        # Masukkan ke FIFO queue
        enqueue_task({
            "type": "CALL",
            "source": "auto",
            "number": auto_call_number,
            "duration": auto_call_duration
        })


# ==================================================
# THREAD POLLING
# ==================================================
def polling_loop():
    global current_interval

    while True:

        # ⛔ STOP TOTAL saat SMS atau CALL
        if sms_session_active or call_active:
            time.sleep(0.2)
            continue

        rssi, dbm, ber = read_csq_once()
        ts = int(time.time())

        if rssi is not None:
            insert_csq(ts, rssi, dbm, ber)
            log(f"[SIGNAL] RSSI={rssi}, dBm={dbm}, BER={ber}")

        time.sleep(current_interval)

def encode_pdu_via_webpdu(smsc, number, message):
    url = "https://www.smsdeliverer.com/online-sms-pdu-encoder.aspx"

    payload = {
        "smsc": smsc.replace("+", ""),
        "number": number.replace("+", ""),
        "message": message,
        "encoding": "7bit",
        "submit": "Encode"
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    resp = requests.post(url, data=payload, headers=headers, timeout=10)

    print("===== WEBPDU HTML START =====")
    print(resp.text[:1500])   # print sebagian dulu
    print("===== WEBPDU HTML END =====")

    raise Exception("STOP HERE FOR DEBUG")

# ==================================================
# API ENDPOINTS
# ==================================================

@app.route("/signal", methods=["GET", "OPTIONS"])
def signal_now():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    rssi, dbm, ber = read_csq_once()
    mode = "dedicated" if call_active else "idle"

    return jsonify({
        "rssi": rssi,
        "dbm": dbm,
        "ber": ber,
        "mode": mode,
    })


@app.route("/history", methods=["GET", "OPTIONS"])
def history():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    limit = request.args.get("limit", 300, type=int)
    start = request.args.get("start", None, type=int)
    end = request.args.get("end", None, type=int)

    return jsonify({"data": get_history(limit, start, end)})


@app.route("/config/interval", methods=["GET"])
def config_interval():
    global current_interval
    new_interval = float(request.args.get("interval", current_interval))

    if not (1 <= new_interval <= 300):
        return jsonify(
            {"status": "error", "message": "Interval 1–300"}
        ), 400

    current_interval = new_interval
    log(f"[CONFIG] Interval diubah menjadi {new_interval}s")
    return jsonify({"status": "ok", "interval": new_interval})


@app.route("/call", methods=["GET"])
def api_call():
    number = request.args.get("number", "+870772001899")
    secs = int(request.args.get("secs", 15))

    enqueue_task({
        "type": "CALL",
        "source": "manual",
        "number": number,
        "duration": secs
    })

    log(f"[API] Manual call queued to {number} ({secs}s)")

    return jsonify({
        "status": "ok",
        "msg": "Call queued"
    })


@app.route("/call/stop", methods=["POST", "OPTIONS"])
def stop_call():
    global call_stop_by_user

    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    call_stop_by_user = True
    log("[CALL] stop_call triggered by user")

    return jsonify({
        "status": "ok",
        "msg": "Stop requested"
    })

@app.route("/config/auto-call", methods=["POST"])
def config_auto_call():
    global auto_call_enabled, auto_call_interval
    global auto_call_number, auto_call_duration

    data = request.json or {}

    auto_call_enabled = bool(data.get("enabled", False))
    auto_call_interval = int(data.get("interval", 60))
    auto_call_number = data.get("number", "")
    auto_call_duration = int(data.get("duration", 15))

    log(
        f"[CONFIG] Auto Call enabled={auto_call_enabled}, "
        f"interval={auto_call_interval}, "
        f"number={auto_call_number}, "
        f"duration={auto_call_duration}"
    )

    save_config()  # 🔥 PENTING

    return jsonify({
        "status": "ok",
        "enabled": auto_call_enabled,
        "interval": auto_call_interval,
        "number": auto_call_number,
        "duration": auto_call_duration
    })



@app.route("/status", methods=["GET"])
def status():
    return jsonify({

        # TASK
        "current_task": current_task,
        "queue_length": task_queue.qsize(),

        # CALL STATE
        "call_active": call_active,
        "call_state": call_state,

        # 🔥 ACTIVE CALL INFO
        "active_call": active_call if call_active else None,

        # SIGNAL
        "interval": current_interval,

        # AUTO CALL
        "auto_call": {
            "enabled": auto_call_enabled,
            "interval": auto_call_interval,
            "number": auto_call_number,
            "duration": auto_call_duration
        },

        # AUTO SMS
        "auto_sms": {
            "enabled": auto_sms_enabled,
            "interval": auto_sms_interval,
            "number": auto_sms_number,
            "message": auto_sms_message
        }
    })

# ==================================================
# DAILY CALL STATISTICS (CSSR TODAY)
# ==================================================
@app.route("/stats/call", methods=["GET"])
def call_stats():

    start_ts = start_of_today_wib()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # total attempt hari ini
    cur.execute("""
        SELECT COUNT(*)
        FROM call_logs
        WHERE timestamp >= ?
    """, (start_ts,))
    total_attempt = cur.fetchone()[0]

    # total success hari ini
    cur.execute("""
        SELECT COUNT(*)
        FROM call_logs
        WHERE timestamp >= ?
        AND status = 'success'
    """, (start_ts,))
    total_success = cur.fetchone()[0]

    conn.close()

    cssr = 0
    if total_attempt > 0:
        cssr = round((total_success / total_attempt) * 100, 2)

    return jsonify({
        "period": "today",
        "total_attempt": total_attempt,
        "total_success": total_success,
        "CSSR_percent": cssr
    })

@app.route("/stats/sms", methods=["GET"])
def sms_stats():

    start_ts = start_of_today_wib()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # total SMS hari ini
    cur.execute("""
        SELECT COUNT(*)
        FROM sms_logs
        WHERE timestamp >= ?
    """, (start_ts,))
    total_attempt = cur.fetchone()[0]

    # total success
    cur.execute("""
        SELECT COUNT(*)
        FROM sms_logs
        WHERE timestamp >= ?
        AND status = 'success'
    """, (start_ts,))
    total_success = cur.fetchone()[0]

    conn.close()

    rate = 0
    if total_attempt > 0:
        rate = round((total_success / total_attempt) * 100, 2)

    return jsonify({
        "period": "today",
        "total_attempt": total_attempt,
        "total_success": total_success,
        "SMS_success_rate": rate
    })


@app.route("/logs/call")
def get_call_logs():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            SELECT timestamp, number, status, cause_code, cause_desc
            FROM call_logs
            ORDER BY id DESC
            LIMIT 50
        """)

        rows = cur.fetchall()
        conn.close()

        return jsonify([
            {
                "time": r[0],
                "number": r[1],
                "status": r[2],
                "cause_code": r[3],
                "cause_desc": r[4]
            }
            for r in rows
        ])

    except Exception as e:
        log(f"[API][ERROR] get_call_logs: {e}")
        return jsonify([])

@app.route("/logs/sms")
def get_sms_logs():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT timestamp, number, status
        FROM sms_logs
        ORDER BY id DESC
        LIMIT 50
    """)

    rows = cur.fetchall()
    conn.close()

    return jsonify([
        {
            "time": r[0],
            "number": r[1],
            "status": r[2]
        }
        for r in rows
    ])

@app.route("/sms/encode", methods=["POST"])
def sms_encode():
    try:
        data = request.json or {}
        number = data.get("number")
        message = data.get("message")

        if not number or not message:
            return jsonify({"status": "error", "msg": "number & message required"}), 400

        # Encode via encode_pdu()
        full_pdu, _ = encode_pdu("+870772001799", number, message)

        # Hitung TPDU length (BENAR untuk AT+CMGS)
        smsc_len = int(full_pdu[:2], 16)
        start_tpdu = (1 + smsc_len) * 2
        tpdu = full_pdu[start_tpdu:]
        tpdu_len = len(tpdu) // 2

        return jsonify({
            "status": "ok",
            "number": number,
            "message": message,
            "pdu": full_pdu,
            "length": tpdu_len
        })

    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500



# ==============================
# TEXT MODE SMS SEND
# ==============================
@app.route("/sms/send", methods=["POST"])
def send_sms():
    data = request.json or {}
    number = data.get("number")
    message = data.get("message")

    if not number or not message:
        return jsonify({
            "status": "error",
            "msg": "number & message required"
        }), 400

    enqueue_task({
        "type": "SMS",
        "source": "manual",
        "number": number,
        "message": message
    })

    log(f"[API] Manual SMS queued to {number}")

    return jsonify({
        "status": "ok",
        "msg": "SMS queued"
    })


def send_sms_internal(number: str, message: str):
    global sms_sending, sms_session_active

    if sms_sending:
        return False

    sms_sending = True
    sms_session_active = True
    log("[SMS] START SMS SESSION")

    try:
        # 🔥 FULL PDU dari encoder
        full_pdu, _ = encode_isatphone_pdu(SMSC_NUMBER, number, message)

        smsc_len = int(full_pdu[0:2], 16)
        tpdu_len = (len(full_pdu) // 2) - (1 + smsc_len)

        log(f"[SMS] TPDU Length : {tpdu_len}")
        log(f"[SMS] FULL PDU    : {full_pdu}")

        with serial_lock:
            ser.reset_input_buffer()

            ser.write(b"AT\r")
            time.sleep(1)
            ser.read_all()

            ser.write(b"AT+CSMS=1\r")
            time.sleep(1)
            ser.read_all()

            ser.write(b"AT+CMGF=0\r")
            time.sleep(1)
            ser.read_all()

            # CMGS
            ser.write(f"AT+CMGS={tpdu_len}\r".encode())

            buf = ""
            start = time.time()

            while time.time() - start < 20:
                if ser.in_waiting:
                    ch = ser.read(1).decode(errors="ignore")
                    buf += ch
                    if ch == ">":
                        break
                time.sleep(0.05)

            if ">" not in buf:
                raise Exception("No '>' prompt from modem")

            ser.write((full_pdu + "\x1A\r").encode())

        # Tunggu respon modem
        time.sleep(2)
        resp = ser.read_all().decode(errors="ignore")
        log(f"[SMS] MODEM RESP: {resp}")

        # Delay async ISAT
        time.sleep(30)

        # ================= LOG SUCCESS =================
        log_sms_db(
            current_task["source"],
            number,
            "success"
        )

        log("[SMS] SEND DONE")
        return True

    except Exception as e:
        log(f"[SMS][ERROR] {e}")

        # ================= LOG FAILED =================
        log_sms_db(
            current_task["source"],
            number,
            "failed"
        )

        return False

    finally:
        sms_session_active = False
        sms_sending = False
        log("[SMS] END SMS SESSION")


def auto_sms_loop():
    global auto_sms_enabled, last_auto_sms_time
    global auto_sms_interval, auto_sms_number, auto_sms_message

    log("[AUTO SMS] Loop started")

    while True:
        time.sleep(1)

        if not auto_sms_enabled:
            continue

        now = time.time()

        if now - last_auto_sms_time < auto_sms_interval:
            continue

        log("[AUTO SMS] Queueing auto sms")

        last_auto_sms_time = now

        enqueue_task({
            "type": "SMS",
            "source": "auto",
            "number": auto_sms_number,
            "message": auto_sms_message
        })


@app.route("/sms/test-encode", methods=["POST"])
def sms_test_encode():
    data = request.json or {}
    number = data.get("number")
    message = data.get("message")

    pdu, length = encode_pdu_via_webpdu(
        SMSC_NUMBER,
        number,
        message
    )

    return jsonify({
        "pdu": pdu,
        "length": length
    })

# ==================================================
# TASK WORKER LOOP (FINAL FIFO)
# ==================================================
def task_worker_loop():
    global current_task

    log("[WORKER] Task worker started")

    while True:
        task = task_queue.get()

        try:
            # ======================
            # SMS TASK
            # ======================
            if task["type"] == "SMS":
                current_task["type"] = "SMS"
                current_task["source"] = task["source"]

                log("[WORKER] Processing SMS")
                send_sms_internal(task["number"], task["message"])

            # ======================
            # CALL TASK
            # ======================
            elif task["type"] == "CALL":
                current_task["type"] = "CALL"
                current_task["source"] = task["source"]

                log("[WORKER] Processing CALL")
                make_call(task["number"], task["duration"])

        except Exception as e:
            log(f"[WORKER][ERROR] {e}")

        finally:
            # RESET STATUS SETELAH TASK SELESAI
            current_task["type"] = None
            current_task["source"] = None

            task_queue.task_done()
            time.sleep(5)   # cooldown modem


@app.route("/sms/check", methods=["GET"])
def sms_check():
    with serial_lock:
        ser.write(b"AT+CREG?\r")
        time.sleep(0.5)
        r1 = ser.read_all().decode(errors="ignore")

        ser.write(b"AT+CSMS?\r")
        time.sleep(0.5)
        r2 = ser.read_all().decode(errors="ignore")

        ser.write(b"AT+CSCA?\r")
        time.sleep(0.5)
        r3 = ser.read_all().decode(errors="ignore")

    return {
        "CREG": r1,
        "CSMS": r2,
        "CSCA": r3
    }

@app.route("/config/auto-sms", methods=["POST"])
def config_auto_sms():
    global auto_sms_enabled, auto_sms_interval
    global auto_sms_number, auto_sms_message

    data = request.json or {}

    auto_sms_enabled = bool(data.get("enabled", False))
    auto_sms_interval = int(data.get("interval", 300))
    auto_sms_number = data.get("number", "")
    auto_sms_message = data.get("message", "")

    log(
        f"[CONFIG] Auto SMS enabled={auto_sms_enabled}, "
        f"interval={auto_sms_interval}, "
        f"number={auto_sms_number}, "
        f"message={auto_sms_message}"
    )

    save_config()  # 🔥 PENTING

    return jsonify({
        "status": "ok",
        "enabled": auto_sms_enabled,
        "interval": auto_sms_interval,
        "number": auto_sms_number,
        "message": auto_sms_message
    })


@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "ISAT Backend aktif"})

# ==============================
# START POLLING THREAD
# ==============================
polling_thread = threading.Thread(
    target=polling_loop,
    daemon=True
)
polling_thread.start()
log("[INIT] Polling thread started")



# ==============================
# START AUTO CALL THREAD
# ==============================
auto_call_thread = threading.Thread(
    target=auto_call_loop,
    daemon=True
)
auto_call_thread.start()
log("[INIT] Auto call thread started")


auto_sms_thread = threading.Thread(
    target=auto_sms_loop,
    daemon=True
)
auto_sms_thread.start()
log("[INIT] Auto SMS thread started")

cleanup_thread = threading.Thread(
    target=cleanup_old_data,
    daemon=True
)
cleanup_thread.start()
log("[INIT] Cleanup thread started")


# ==============================
# START TASK WORKER (FIFO)
# ==============================
task_worker_thread = threading.Thread(
    target=task_worker_loop,
    daemon=True
)
task_worker_thread.start()
log("[INIT] Task worker started")



# ==================================================
# MAIN
# ==================================================
if __name__ == "__main__":
    log(f"[START] Flask running on 0.0.0.0:{FLASK_PORT}")
    app.run(host="0.0.0.0", port=FLASK_PORT)
+870772001799
