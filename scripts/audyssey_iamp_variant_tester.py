"""Probe AVR Audyssey TCP protocol — what SET_SETDAT IAmp payloads ACK vs NACK.

Used during the 2026-04-27 protocol-discovery session to find the working
n_pos=1 format for combined IAmp pushes. Run against a Denon X3800H to map
which payload variants the firmware accepts.

Findings codified in scripts/audyssey_push_full_iamp.py.
"""
import json, socket, struct, time, sys

HOST, PORT = "192.168.1.209", 1256
HEADER_LEN, CMD_LEN = 9, 10


def frame(cmd: str, data: bytes = b"") -> bytes:
    cb = cmd.encode("ascii")
    total = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total)
    buf += b"\x00\x00"
    buf += cb
    buf.append(0)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream: bytearray) -> list:
    out = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if len(stream) < total_len:
            break
        cmd = bytes(stream[5:15]).decode("ascii", errors="replace").strip()
        dlen = struct.unpack(">H", bytes(stream[16:18]))[0]
        data = bytes(stream[18:18 + dlen])
        out.append((cmd, data))
        del stream[:total_len]
    return out


def main():
    ady = json.load(open("/storage/1.ady"))
    chans = [c["commandId"] for c in ady["detectedChannels"] if c.get("commandId")]
    distance = {c["commandId"]: round(float(c.get("customDistance", 0)) * 100) for c in ady["detectedChannels"] if c.get("commandId")}

    # Distance baseline (3 positions)
    n_pos = 3
    dist_array = [dict(distance) for _ in range(n_pos)]

    # variants to try
    variants = []

    # 1. Distance only (known to ACK)
    variants.append(("baseline_distance_only", {"Distance": dist_array}))

    # 2. Distance + AudyDynEq (scalar bool)
    variants.append(("distance_plus_dyneq", {"Distance": dist_array, "AudyDynEq": False}))

    # 3. Distance + AudyEqRef (scalar int)
    variants.append(("distance_plus_eqref", {"Distance": dist_array, "AudyEqRef": 0}))

    # 4. Distance + AudyDynEq + AudyEqRef
    variants.append(("distance_plus_both_flags", {
        "Distance": dist_array, "AudyDynEq": False, "AudyEqRef": 0
    }))

    # 5. Distance + ChLevel (zeros) as dB*2 (int -24..24)
    chl_zero = [{c: 0 for c in chans} for _ in range(n_pos)]
    variants.append(("distance_plus_chlevel_int_zero", {
        "Distance": dist_array, "ChLevel": chl_zero
    }))

    # 6. Distance + Crossover all "F"
    xover_all_F = [{c: "F" for c in chans} for _ in range(n_pos)]
    variants.append(("distance_plus_crossover_all_F", {
        "Distance": dist_array, "Crossover": xover_all_F
    }))

    # 7. Distance + SpConfig (S for non-sub, E for SW)
    spconf = [{c: ("E" if c.startswith("SW") else "S") for c in chans} for _ in range(n_pos)]
    variants.append(("distance_plus_spconfig", {
        "Distance": dist_array, "SpConfig": spconf
    }))

    # 8. n_pos=1
    variants.append(("distance_n_pos_1", {"Distance": [dict(distance)]}))

    # 9. SET only AudyDynEq
    variants.append(("only_dyneq", {"AudyDynEq": False}))

    # 10. SET only Distance, AudyFinFlg=NotFin (per ratbuddyssey: "NotFin" before commit)
    variants.append(("distance_with_notfin", {
        "Distance": dist_array, "AudyFinFlg": "NotFin"
    }))

    sock = socket.create_connection((HOST, PORT), timeout=8)
    sock.settimeout(2.0)
    rxbuf = bytearray()

    def drain(seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                c = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not c:
                break
            rxbuf.extend(c)

    try:
        sock.sendall(frame("ENTER_AUDY"))
        drain(2.0)
        rxbuf.clear()

        for name, payload in variants:
            body = json.dumps(payload, separators=(",", ":")).encode()
            sock.sendall(frame("SET_SETDAT", body))
            drain(2.0)
            rx_frames = parse_frames(rxbuf)
            rxbuf.clear()
            verdict = "?"
            for cmd, data in rx_frames:
                txt = data.decode("ascii", errors="replace")
                if "ACK" in txt and "NACK" not in txt:
                    verdict = "ACK"
                    break
                if "NACK" in txt:
                    verdict = "NACK"
                    break
            print(f"{verdict:5}  {name:40s}  bodylen={len(body)}")

        sock.sendall(frame("EXIT_AUDMD"))
        drain(0.5)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
