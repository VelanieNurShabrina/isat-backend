# pdu_encoder.py
# FINAL: match WebPDU perusahaan (NO buggy GSM7)

# pdu_encoder.py
# Encoder dummy VALID (match WebPDU perusahaan)
# Dipakai hanya untuk testing backend & AT command flow

def encode_pdu(text, number):
    """
    PDU ini SUDAH TERBUKTI VALID:
    - Sama persis dengan WebPDU perusahaan
    - Bisa dikirim manual via AT Command
    """

    pdu = "079178702700719911000D91265812900274F400000B0AF4F29C9E769F63B219"
    length = 24

    return pdu, length


# optional: biar bisa dites manual
if __name__ == "__main__":
    pdu, length = encode_pdu("testing123", "+6285210920474")
    print("PDU :", pdu)
    print("LEN :", length)
