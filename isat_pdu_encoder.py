# =========================================================
# isat_pdu_encoder.py
# FINAL – ISATPHONE COMPATIBLE PDU ENCODER
# - GSM 7-bit
# - Valid AT+CMGS
# - Length(TPDU only)
# =========================================================

def swap_nibbles(s):
    if len(s) % 2 == 1:
        s += "F"
    return "".join(s[i+1] + s[i] for i in range(0, len(s), 2))


def gsm7_pack(text):
    septets = [ord(c) & 0x7F for c in text]
    bits = 0
    bit_len = 0
    out = []

    for s in septets:
        bits |= s << bit_len
        bit_len += 7
        while bit_len >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            bit_len -= 8

    if bit_len > 0:
        out.append(bits & 0xFF)

    return "".join(f"{b:02X}" for b in out)


def encode_isatphone_pdu(smsc, dest, text):
    # ===== SMSC =====
    smsc_digits = smsc.replace("+", "")
    smsc_swapped = swap_nibbles(smsc_digits)
    smsc_len = len(smsc_swapped) // 2 + 1
    smsc_pdu = f"{smsc_len:02X}91{smsc_swapped}"

    # ===== DESTINATION =====
    dest_digits = dest.replace("+", "")
    dest_len = len(dest_digits)
    dest_swapped = swap_nibbles(dest_digits)

    # ===== USER DATA =====
    ud_hex = gsm7_pack(text)
    ud_len = len(text)

    # ===== TPDU =====
    tpdu = (
        "11"                    # SMS-SUBMIT
        "00"                    # MR
        f"{dest_len:02X}"       # Destination length
        "91"                    # TON/NPI
        f"{dest_swapped}"       # Destination number
        "00"                    # PID
        "00"                    # DCS
        "0B"                    # VP
        f"{ud_len:02X}"         # UDL
        f"{ud_hex}"             # UD
    )

    pdu = smsc_pdu + tpdu

    smsc_bytes = int(pdu[0:2], 16) + 1
    length = (len(pdu) // 2) - smsc_bytes

    return pdu, length


# ================= TEST =================
# ================= TEST =================
if __name__ == "__main__":
    pdu, length = encode_isatphone_pdu(
        "+870772001799",
        "+6285210920474",
        "halo velanie"
    )
    print("PDU :", pdu)
    print("LEN :", length)
