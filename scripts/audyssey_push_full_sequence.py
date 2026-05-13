"""Full ratbuddyssey-style upload sequence: push IAmp + IAudy + DisFil +
INIT_COEFS + CoefDt + Fin in one TCP session.

Hypothesis: the MultEQ-off-after-commit side effect we see with IAmp-only
commits may be because the AVR sees "Fin" without the rest of the upload
sequence having run, and panics into a "filters invalid, disable
MultEQ" state. If we send the full sequence the AVR might keep MultEQ
on, and the per-channel applied-delay 65ms cap might not be engaged.

USAGE
    python audyssey_push_full_sequence.py 192.168.1.209 \\
        --fl 100 --c 4.05 --fr 4.05 --sw1 13.5 \\
        --commit
"""
from __future__ import annotations
import argparse, json, socket, struct, sys, time

DEFAULT_PORT = 1256
HEADER_LEN, CMD_LEN = 9, 10

CHANNELS = ["FL", "C", "FR", "SLA", "SRA", "TFL", "TFR", "TRL", "TRR", "SW1"]
CH_HEADER_BITS = {"FL": 0x000, "C": 0x100, "FR": 0x200, "SRA": 0x300,
                  "SLA": 0xC00, "SW1": 0xD00}
RATE_BITS = {"32k": 0x00000, "44.1k": 0x10000, "48k": 0x20000}
CURVE_BITS = {"Audy": 0x00000000, "Flat": 0x01000000}


def build_frame(cmd, data=b"", cur=0, tot=0):
    cb = cmd.encode("ascii")
    tl = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", tl)
    buf.append(cur & 0xFF)
    buf.append(tot & 0xFF)
    buf += cb
    buf.append(0)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream):
    out = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        tl = struct.unpack(">H", bytes(stream[1:3]))[0]
        if tl < 19 or tl > 0xFFFF:
            del stream[0]
            continue
        if len(stream) < tl:
            break
        dlen = struct.unpack(">H", bytes(stream[16:18]))[0]
        out.append({"cmd": bytes(stream[5:15]).decode("ascii", errors="replace").strip(),
                    "data": bytes(stream[18:18 + dlen]),
                    "dir": chr(stream[0])})
        del stream[:tl]
    return out


class Sender:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=6.0)
        self.s.settimeout(2.0)
        self.rx = bytearray()

    def drain(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            try:
                self.s.settimeout(max(0.05, end - time.monotonic()))
                c = self.s.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            if not c:
                return
            self.rx.extend(c)

    def send(self, label, cmd, data=b"", cur=0, tot=0, wait=0.4, verbose=True):
        self.s.sendall(build_frame(cmd, data, cur, tot))
        self.drain(wait)
        comm = "TIMEOUT"
        for f in parse_frames(self.rx):
            if f["dir"] == "R":
                try:
                    obj = json.loads(f["data"].decode("ascii", errors="replace"))
                    c = obj.get("Comm") if isinstance(obj, dict) else None
                    if c:
                        comm = c
                        break
                except Exception:
                    comm = "NON_JSON"
                    break
        self.rx.clear()
        if verbose:
            m = {"ACK": "✓", "NACK": "✗", "TIMEOUT": "?"}.get(comm, "·")
            print(f"  [{m}] {label:55s} → {comm}")
        return comm

    def close(self):
        self.s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--fl", type=float, default=4.05)
    ap.add_argument("--c", type=float, default=4.05)
    ap.add_argument("--fr", type=float, default=4.05)
    ap.add_argument("--sla", type=float, default=4.05)
    ap.add_argument("--sra", type=float, default=4.05)
    ap.add_argument("--tfl", type=float, default=4.05)
    ap.add_argument("--tfr", type=float, default=4.05)
    ap.add_argument("--trl", type=float, default=4.05)
    ap.add_argument("--trr", type=float, default=4.05)
    ap.add_argument("--sw1", type=float, default=13.5)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--fl-delay-adj", type=float, default=None,
                    help='per-channel delayAdjustment for FL (units TBD; sent as "X.000000" string)')
    ap.add_argument("--system-delay", type=int, default=None,
                    help="top-level systemDelay scalar")
    args = ap.parse_args()

    distances_m = {"FL": args.fl, "C": args.c, "FR": args.fr,
                    "SLA": args.sla, "SRA": args.sra,
                    "TFL": args.tfl, "TFR": args.tfr,
                    "TRL": args.trl, "TRR": args.trr,
                    "SW1": args.sw1}
    distances_cm = {ch: round(m * 100) for ch, m in distances_m.items()}
    chlevel = {ch: 0 for ch in distances_cm}
    crossover = {ch: ("80" if ch != "SW1" else " ") for ch in distances_cm}
    spconfig = {ch: ("E" if ch == "SW1" else "S") for ch in distances_cm}

    iamp = {
        "Distance":  [dict(distances_cm)],
        "ChLevel":   [dict(chlevel)],
        "Crossover": [dict(crossover)],
        "SpConfig":  [dict(spconfig)],
        "AudyDynEq": False,
        "AudyEqRef": 0,
    }
    if args.fl_delay_adj is not None:
        delay_adj = {ch: "0.000000" for ch in distances_cm}
        delay_adj["FL"] = f"{args.fl_delay_adj:.6f}"
        iamp["delayAdjustment"] = [dict(delay_adj)]
        # also try integer form so AVR can pick whichever it prefers
        delay_adj_int = {ch: 0 for ch in distances_cm}
        delay_adj_int["FL"] = int(round(args.fl_delay_adj))
        # we'll print both forms for debugging
    if args.system_delay is not None:
        iamp["systemDelay"] = args.system_delay
    iaudy = {"EnMultEQ": "On", "EnDynEq": "Off",
             "DynEqOff": "0", "EnDynVol": "Off", "DynVolSet": "0"}

    print(f"=== full-sequence push to {args.host}:{args.port} ===")
    print(f"distances (cm): {distances_cm}")
    print(f"commit: {args.commit}")
    print()

    s = Sender(args.host, args.port)

    # 1. ENTER_AUDY
    s.send("ENTER_AUDY", "ENTER_AUDY", wait=2.0)

    # 2. SET_SETDAT IAmp
    body = json.dumps(iamp, separators=(",", ":")).encode()
    s.send(f"SET_SETDAT IAmp ({len(body)}B)", "SET_SETDAT", body, wait=2.0)

    # 3. SET_SETDAT IAudy
    body = json.dumps(iaudy, separators=(",", ":")).encode()
    s.send(f"SET_SETDAT IAudy ({len(body)}B)", "SET_SETDAT", body, wait=1.0)

    # 4. SET_DISFIL × N (one per (channel, EqType) — empty arrays which ACK)
    for ch in CHANNELS:
        for eq in ("Audy", "Flat"):
            body = json.dumps({"EqType": eq, "ChData": ch,
                               "FilData": [], "DispData": []},
                              separators=(",", ":")).encode()
            s.send(f"SET_DISFIL {ch}/{eq}", "SET_DISFIL", body, wait=0.4)

    # 5. INIT_COEFS empty
    s.send("INIT_COEFS (no-reply expected)", "INIT_COEFS", b"", wait=0.4)

    # 6. SET_COEFDT × N — push N=512 zero-float32 for each (ch, rate, curve)
    n = 512
    coeff_bytes = struct.pack(f">{n}f", *[0.0] * n)
    for ch in CHANNELS:
        if ch not in CH_HEADER_BITS:
            continue
        for rate in ("44.1k", "32k", "48k"):
            for curve in ("Audy", "Flat"):
                header = CH_HEADER_BITS[ch] | RATE_BITS[rate] | CURVE_BITS[curve]
                body = struct.pack(">i", header) + coeff_bytes
                chunks = [body[i:i + 512] for i in range(0, len(body), 512)]
                tot = len(chunks) - 1
                for i, chunk in enumerate(chunks):
                    s.send(f"CoefDt {ch}/{rate}/{curve} {i}/{tot}",
                           "SET_COEFDT", chunk, cur=i, tot=tot, wait=0.2,
                           verbose=(i == 0 or i == tot))

    # 7. Fin
    if args.commit:
        s.send("⚠  SET_SETDAT AudyFinFlg=Fin (COMMIT)", "SET_SETDAT",
               b'{"AudyFinFlg":"Fin"}', wait=2.0)

    # 8. EXIT_AUDMD
    s.send("EXIT_AUDMD", "EXIT_AUDMD", wait=1.0)
    s.close()
    print("done.")


if __name__ == "__main__":
    main()
