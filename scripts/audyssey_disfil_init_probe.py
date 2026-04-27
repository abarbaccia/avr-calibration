"""Tighter probe: SET_DISFIL length/value rules + INIT_COEFS body shape."""
from __future__ import annotations
import argparse, json, socket, struct, sys, time

DEFAULT_PORT = 1256
HEADER_LEN, CMD_LEN = 9, 10


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


def parse(stream):
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


class P:
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

    def send(self, label, cmd, data=b"", cur=0, tot=0, wait=1.5):
        self.s.sendall(build_frame(cmd, data, cur, tot))
        self.drain(wait)
        comm = "TIMEOUT"; rx = b""
        for f in parse(self.rx):
            if f["dir"] == "R":
                try:
                    obj = json.loads(f["data"].decode("ascii", errors="replace"))
                    if isinstance(obj, dict) and "Comm" in obj:
                        comm = obj["Comm"]; rx = f["data"]; break
                except Exception:
                    rx = f["data"]; comm = "NON_JSON"; break
        self.rx.clear()
        m = {"ACK": "✓", "NACK": "✗", "TIMEOUT": "?"}.get(comm, "·")
        print(f"  [{m}] {label:60s} → {comm:8s} rx={rx[:80]!r}")
        return comm

    def close(self): self.s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host"); ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    p = P(args.host, args.port)
    p.send("ENTER_AUDY", "ENTER_AUDY", wait=2.0)

    print("\n--- SET_DISFIL: small lengths to find acceptance boundary ---")
    for n in (1, 2, 4, 8, 16, 31, 32, 33, 63, 64, 65, 100, 128):
        body = json.dumps({"EqType": "Audy", "ChData": "FL",
                           "FilData": [0]*n, "DispData": [0]*n}, separators=(",", ":")).encode("ascii")
        p.send(f"len={n} (zeros)", "SET_DISFIL", body)

    print("\n--- SET_DISFIL: same length but only FilData populated ---")
    for n in (32, 64, 128):
        body = json.dumps({"EqType": "Audy", "ChData": "FL",
                           "FilData": [0]*n, "DispData": []}, separators=(",", ":")).encode("ascii")
        p.send(f"FilData={n} DispData=0", "SET_DISFIL", body)

    print("\n--- SET_DISFIL: only DispData populated ---")
    for n in (32, 64, 128):
        body = json.dumps({"EqType": "Audy", "ChData": "FL",
                           "FilData": [], "DispData": [0]*n}, separators=(",", ":")).encode("ascii")
        p.send(f"FilData=0 DispData={n}", "SET_DISFIL", body)

    print("\n--- SET_DISFIL: signed vs unsigned, value ranges ---")
    body = json.dumps({"EqType": "Audy", "ChData": "FL",
                       "FilData": [-128]*32, "DispData": [-128]*32}, separators=(",", ":")).encode("ascii")
    p.send("len=32 all -128 (sbyte min)", "SET_DISFIL", body)
    body = json.dumps({"EqType": "Audy", "ChData": "FL",
                       "FilData": [127]*32, "DispData": [127]*32}, separators=(",", ":")).encode("ascii")
    p.send("len=32 all 127 (sbyte max)", "SET_DISFIL", body)
    body = json.dumps({"EqType": "Audy", "ChData": "FL",
                       "FilData": [255]*32, "DispData": [255]*32}, separators=(",", ":")).encode("ascii")
    p.send("len=32 all 255 (overflow sbyte)", "SET_DISFIL", body)

    print("\n--- SET_DISFIL: EqType variants ---")
    for eq in ("Audy", "Flat", "audy", "AUDY", "Reference"):
        body = json.dumps({"EqType": eq, "ChData": "FL", "FilData": [], "DispData": []},
                          separators=(",", ":")).encode("ascii")
        p.send(f"EqType={eq!r}", "SET_DISFIL", body)

    print("\n--- INIT_COEFS body variants ---")
    p.send("empty", "INIT_COEFS", b"", wait=2.5)
    p.send("'?' query", "INIT_COEFS", b'{"":"?"}', wait=2.5)
    p.send("Comm:?", "INIT_COEFS", b'{"Comm":"?"}', wait=2.5)
    p.send("Channel:FL", "INIT_COEFS", b'{"Channel":"FL"}', wait=2.5)
    p.send("ChData:FL", "INIT_COEFS", b'{"ChData":"FL"}', wait=2.5)
    p.send("ChData:All", "INIT_COEFS", b'{"ChData":"All"}', wait=2.5)
    p.send("ChData with ALL channels", "INIT_COEFS",
           b'{"ChData":["FL","C","FR","SLA","SRA","TFL","TFR","TRL","TRR","SW1"]}', wait=2.5)
    p.send("just newline", "INIT_COEFS", b"\n", wait=2.5)

    p.send("EXIT_AUDMD", "EXIT_AUDMD")
    p.close()


if __name__ == "__main__":
    main()
