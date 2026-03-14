"""
Crowd visual novel engine decryption (X-Change 2R, 1998).

Reverse-engineered from xc2r.exe disassembly.

Key finding: there is NO buffer seeding/evolution step. The decryption loop
uses the key string directly as a lookup table, with a rolling key_byte
and counter that update whenever the index wraps to 0.

Decrypt function at 0x407B20:
  - arg1 = file path
  - arg2 = key data pointer ("crowd script yeah !")
  - arg3 = 0x12 = 18 (modulo size, excludes trailing '!')
  - Cipher byte transform at 0x4079E0: (a|b) & ~(a&b) == XOR

Decryption loop:
  key_byte = 0, counter = 0
  for each byte i:
    idx = (key_byte + i) % 18
    k = key[idx] | (key_byte & counter)     # byte-level ops
    decrypted[i] = encrypted[i] ^ k
    if idx == 0:
      key_byte = key[(old_key_byte + counter) % 18]
      counter += 1
"""

KEY = b"crowd script yeah !"
MOD = 18  # 0x12 — indices 0..17, the '!' at index 18 is never used


def decrypt(data: bytes) -> bytearray:
    """Decrypt a Crowd .sce file."""
    key = KEY
    out = bytearray(len(data))

    key_byte = 0    # ebx (bl)
    counter = 0     # [esp+0x18], used as byte

    for i in range(len(data)):
        idx = (key_byte + i) % MOD
        k = key[idx] | ((key_byte & counter) & 0xFF)
        out[i] = data[i] ^ (k & 0xFF)

        if idx == 0:
            new_idx = (key_byte + counter) % MOD
            key_byte = key[new_idx]
            counter = (counter + 1) & 0xFFFFFFFF  # dword counter, but &0xFF in use

    return out


def encrypt(data: bytes) -> bytearray:
    """Encrypt is the same operation (XOR is symmetric)."""
    return decrypt(data)


if __name__ == "__main__":
    import sys

    sce_path = sys.argv[1] if len(sys.argv) > 1 else r"e:\hgames\xchange 2r\xc2r.sce"

    with open(sce_path, "rb") as f:
        enc = f.read()

    dec = decrypt(enc)

    print(f"File size: {len(enc)} bytes")
    print(f"Encrypted (first 64): {enc[:64].hex(' ')}")
    print(f"Decrypted (first 64): {dec[:64].hex(' ')}")
    print()

    # Try to decode as SJIS
    try:
        text = dec[:256].decode("shift_jis", errors="replace")
        print(f"SJIS decode (first 256 bytes):")
        print(text[:200])
    except Exception as e:
        print(f"SJIS decode failed: {e}")

    print()

    # Check for common patterns
    if dec[:4] in (b"CRD\x00", b"CRWD"):
        print("*** Found CRD/CRWD header! ***")
    if all(0x20 <= b < 0x7F or b in (0x0D, 0x0A, 0x09, 0x00) for b in dec[:32]):
        print("*** First 32 bytes look like ASCII/text! ***")

    # Check for any recognizable structure
    print(f"First 16 bytes as chars: {dec[:16]}")

    # Write decrypted output
    out_path = sce_path.replace(".sce", "_decrypted.bin")
    with open(out_path, "wb") as f:
        f.write(dec)
    print(f"\nDecrypted written to: {out_path}")
