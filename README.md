# ISATPhone Backend Monitoring System

This project is a backend system for monitoring communication using ISATPHONE modem via AT Commands.

---

## 🚀 Features

### 📞 Call Monitoring

* Detect call status: `success` / `failed`
* Parse call state using `AT+CLCC`
* Validate connection (DIALING → ACTIVE)
* Cause code handling:

  * From `+SKCCSI` (index 5)
  * Fallback if not available
* CSSR (Call Success Rate) calculation

### 💬 SMS Monitoring

* Send SMS using PDU mode
* Encoder modules:

  * `pdu_encoder.py`
  * `isat_pdu_encoder.py`
* SMS delivery status (success / failed)
* SMS CSSR calculation

### 📶 Signal Monitoring

* Monitor signal strength (`AT+CSQ`)
* Convert to dBm
* Store for history tracking

### 📊 Dashboard Support

* Call logs
* SMS logs
* Statistics (CSSR)
* History data (call, SMS, signal)

### ⚙️ System Features

* FIFO queue for task processing
* Auto call & auto SMS
* Configurable polling interval
* Multi-threading:

  * Call monitoring
  * SMS processing
  * Signal polling
  * Cleanup task

---

## 🧩 Tech Stack

* Python
* Flask
* PySerial
* SQLite

---

## 📂 Project Structure

* `isat_service.py` → Main backend service
* `pdu_encoder.py` → General PDU encoder
* `isat_pdu_encoder.py` → ISATPhone encoder
* `config.json` → Configuration file
* `init_db.py` → Database initialization

---

## ▶️ How to Run

```bash
pip install -r requirements.txt
python isat_service.py
```

---

## 🧠 Call Validation Logic

A call is considered **SUCCESS** if:

* There is a transition from `DIALING` to `ACTIVE`
* The connection is properly established

A call is considered **FAILED** if:

* It never reaches ACTIVE state
* It ends prematurely or times out

---

## 📌 Cause Code Handling

* Extracted from:

  ```
  +SKCCSI: ...
  ```
* If not available:

  * Fallback logic is applied

---

## 👩‍💻 Author

Velanie Nur Shabrina
