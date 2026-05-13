"""Live-push validation test: push synthetic coefficients on ONE channel,
hold the Audyssey session open, prompt user to observe, then EXIT without
committing.

Goal: prove that custom values pushed via SET_COEFDT actually change the
AVR's audio output — the empirical proof that "the amp utilizes our
pushed values".

What this does:
    ENTER_AUDY
    SET_DISFIL <channel>/Audy with empty arrays                (preregister)
    INIT_COEFS empty body                                      (don't wait for ACK; AVR doesn't reply)
    SET_COEFDT <channel>/44.1k/Audy with N float32 coefficients
    SET_COEFDT <channel>/44.1k/Flat with same                  (some firmware needs both)
    SET_COEFDT <channel>/32k/Audy
    SET_COEFDT <channel>/32k/Flat
    SET_COEFDT <channel>/48k/Audy
    SET_COEFDT <channel>/48k/Flat
    --- pause for user to observe ---
    EXIT_AUDMD     (NO commit; volatile state should revert on power cycle or app re-import)

USAGE
    python audyssey_live_push_test.py 192.168.1.209 \\
        --channel FL --pattern silence --length 512

PATTERNS
    silence  : all zeros (should silence the channel via MultEQ)
    impulse  : [1.0, 0, 0, ..., 0] (delta — pass-through if FIR)
    one-shot : [0, ..., 1.0, ..., 0] (delta in middle — pass-through with delay)
    noise    : pseudo-random small values (should make it sound noisy/comb)
"""
from __future__ import annotations
import argparse, json, socket, struct, sys, time

DEFAULT_PORT = 1256
HEADER_LEN, CMD_LEN = 9, 10

CHANNEL_BITS = {"FL": 0x000, "C": 0x100, "FR": 0x200, "SRA": 0x300,
                "SLA": 0xC00, "SW1": 0xD00}
RATE_BITS = {"32k": 0x00000, "44.1k": 0x10000, "48k": 0x20000}
CURVE_BITS = {"Audy": 0x00000000, "Flat": 0x01000000}


def build_frame(cmd, data=b"", cur=0, tot=0):
    cb = cmd.encode("ascii")
    tl = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T")); buf += struct.pack(">H", tl)
    buf.append(cur & 0xFF); buf.append(tot & 0xFF)
    buf += cb; buf.append(0)
    buf += struct.pack(">H", len(data)); buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream):
    out = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")): del stream[0]; continue
        tl = struct.unpack(">H", bytes(stream[1:3]))[0]
        if tl < 19 or tl > 0xFFFF: del stream[0]; continue
        if len(stream) < tl: break
        dlen = struct.unpack(">H", bytes(stream[16:18]))[0]
        out.append({"cmd": bytes(stream[5:15]).decode("ascii", errors="replace").strip(),
                    "data": bytes(stream[18:18+dlen]), "dir": chr(stream[0])})
        del stream[:tl]
    return out


def make_pattern(name: str, length: int) -> list[float]:
    if name == "silence":
        return [0.0] * length
    if name == "impulse":
        return [1.0] + [0.0] * (length - 1)
    if name == "one-shot":
        v = [0.0] * length
        v[length // 2] = 1.0
        return v
    if name == "noise":
        import random
        random.seed(42)
        return [random.uniform(-0.05, 0.05) for _ in range(length)]
    raise ValueError(f"unknown pattern {name!r}")


def encode_f32(values: list[float]) -> bytes:
    return struct.pack(f">{len(values)}f", *values)


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
            except (socket.timeout, TimeoutError): return
            if not c: return
            self.rx.extend(c)

    def send(self, label, cmd, data=b"", cur=0, tot=0, wait=0.5):
        self.s.sendall(build_frame(cmd, data, cur, tot))
        self.drain(wait)
        comm = "TIMEOUT"
        for f in parse_frames(self.rx):
            if f["dir"] == "R":
                try:
                    obj = json.loads(f["data"].decode("ascii", errors="replace"))
                    c = obj.get("Comm") if isinstance(obj, dict) else None
                    if c: comm = c; break
                except Exception:
                    comm = "NON_JSON"; break
        self.rx.clear()
        m = {"ACK": "✓", "NACK": "✗", "TIMEOUT": "?"}.get(comm, "·")
        print(f"  [{m}] {label:55s} → {comm}")
        return comm

    def push_coefdt_blob(self, channel: str, rate: str, curve: str,
                         coeffs: list[float]) -> str:
        header = CHANNEL_BITS[channel] | RATE_BITS[rate] | CURVE_BITS[curve]
        body = struct.pack(">i", header) + encode_f32(coeffs)
        chunks = [body[i:i+512] for i in range(0, len(body), 512)]
        tot = len(chunks) - 1
        last = "?"
        for i, ch in enumerate(chunks):
            last = self.send(f"CoefDt {channel}/{rate}/{curve} pkt {i}/{tot}",
                             "SET_COEFDT", ch, cur=i, tot=tot, wait=0.3)
            if last == "NACK": break
        return last

    def close(self): self.s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--channel", default="FL", choices=list(CHANNEL_BITS))
    ap.add_argument("--pattern", default="silence",
                    choices=("silence", "impulse", "one-shot", "noise"))
    ap.add_argument("--length", type=int, default=512)
    ap.add_argument("--rates", default="44.1k,32k,48k",
                    help="comma-separated rates to push")
    ap.add_argument("--curves", default="Audy,Flat",
                    help="comma-separated curves to push")
    ap.add_argument("--commit", action="store_true",
                    help="send AudyFinFlg=Fin (DESTRUCTIVE — wipes original Audyssey)")
    ap.add_argument("--hold", type=float, default=0.0,
                    help="seconds to hold session open before EXIT (0 = interactive prompt)")
    args = ap.parse_args()

    coeffs = make_pattern(args.pattern, args.length)
    rates = [r.strip() for r in args.rates.split(",")]
    curves = [c.strip() for c in args.curves.split(",")]

    print(f"=== live push test ===")
    print(f"target:   {args.host}:{args.port}")
    print(f"channel:  {args.channel}")
    print(f"pattern:  {args.pattern} (len={args.length})")
    print(f"rates:    {rates}")
    print(f"curves:   {curves}")
    print(f"commit:   {args.commit}")

    s = Sender(args.host, args.port)
    s.send("ENTER_AUDY", "ENTER_AUDY", wait=2.0)

    # Preregister via SET_DISFIL with empty arrays (we know this ACKs)
    body = json.dumps({"EqType": "Audy", "ChData": args.channel,
                       "FilData": [], "DispData": []}, separators=(",", ":")).encode()
    s.send(f"SET_DISFIL {args.channel}/Audy empty", "SET_DISFIL", body)
    body = json.dumps({"EqType": "Flat", "ChData": args.channel,
                       "FilData": [], "DispData": []}, separators=(",", ":")).encode()
    s.send(f"SET_DISFIL {args.channel}/Flat empty", "SET_DISFIL", body)

    # INIT_COEFS — AVR doesn't reply, just send and proceed
    s.send("INIT_COEFS (no-reply expected)", "INIT_COEFS", b"", wait=0.3)

    # Push coefficients across all selected (rate, curve) tuples
    for rate in rates:
        for curve in curves:
            s.push_coefdt_blob(args.channel, rate, curve, coeffs)

    if args.commit:
        s.send("⚠  SET_SETDAT AudyFinFlg=Fin (COMMIT)", "SET_SETDAT",
               b'{"AudyFinFlg":"Fin"}', wait=2.0)

    print()
    print("=" * 64)
    print(f"  PUSH COMPLETE — pattern={args.pattern} on {args.channel}")
    print(f"  Listen / measure now.")
    print("=" * 64)
    if args.hold > 0:
        print(f"holding session open for {args.hold}s...")
        time.sleep(args.hold)
    else:
        input("press Enter to EXIT_AUDMD and close session... ")

    s.send("EXIT_AUDMD", "EXIT_AUDMD")
    s.close()
    print("done.")


if __name__ == "__main__":
    main()
