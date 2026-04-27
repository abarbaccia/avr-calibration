"""Probe SET_SETDAT for `delayAdjustment` / `SystemDelay` / `Delay` fields.

The .ady file has `delayAdjustment` per channel (string float) and a
top-level `systemDelay` int — neither tested by ratbuddyssey IAmp probes.
Find which field names ACK as part of the IAmp payload and learn whether
larger-than-65ms delay can be pushed through them."""
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
        out.append({"data": bytes(stream[18:18+dlen]), "dir": chr(stream[0])})
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

    def send(self, label, cmd, data=b"", wait=1.5):
        self.s.sendall(build_frame(cmd, data))
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
        print(f"  [{m}] {label:60s} → {comm:8s} rx={rx[:90]!r}")
        return comm

    def close(self): self.s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host"); ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    p = P(args.host, args.port)
    p.send("ENTER_AUDY", "ENTER_AUDY", wait=2.0)

    # Read current state to see what fields the AVR knows about
    print("\n--- read existing state ---")
    p.send("GET_AVRINF", "GET_AVRINF", wait=2.0)
    p.send("GET_AVRSTS", "GET_AVRSTS", wait=2.0)

    # Try SET_SETDAT query for various delay-shaped names
    print("\n--- query for Delay-like fields ---")
    for q in ('{"Delay":"?"}',
              '{"DelayAdjustment":"?"}',
              '{"delayAdjustment":"?"}',
              '{"SystemDelay":"?"}',
              '{"systemDelay":"?"}',
              '{"LipSync":"?"}',
              '{"AudioDelay":"?"}',
              '{"ChDelay":"?"}',
              '{"ChDly":"?"}',
              '{"AudyDelay":"?"}'):
        p.send(f"query {q}", "SET_SETDAT", q.encode())

    # Try as part of IAmp combined payload
    print("\n--- write Delay-shaped fields combined with Distance ---")
    base_dist = {"FL": 405, "C": 405, "FR": 405, "SLA": 405, "SRA": 405,
                 "TFL": 405, "TFR": 405, "TRL": 405, "TRR": 405, "SW1": 1350}

    # Try Delay as per-channel array (mirrors Distance shape)
    body = json.dumps({"Distance": [dict(base_dist)],
                       "Delay": [{ch: 100 for ch in base_dist}]},
                      separators=(",", ":")).encode()
    p.send("Distance + Delay [{FL:100, ...}] (int)", "SET_SETDAT", body)

    body = json.dumps({"Distance": [dict(base_dist)],
                       "Delay": [{ch: "100.000000" for ch in base_dist}]},
                      separators=(",", ":")).encode()
    p.send("Distance + Delay [{FL:'100.0000', ...}] (str)", "SET_SETDAT", body)

    body = json.dumps({"Distance": [dict(base_dist)],
                       "DelayAdjustment": [{ch: 100 for ch in base_dist}]},
                      separators=(",", ":")).encode()
    p.send("Distance + DelayAdjustment [{FL:100, ...}]", "SET_SETDAT", body)

    body = json.dumps({"Distance": [dict(base_dist)],
                       "delayAdjustment": [{ch: "100.000000" for ch in base_dist}]},
                      separators=(",", ":")).encode()
    p.send("Distance + delayAdjustment [{FL:'100.0', ...}]", "SET_SETDAT", body)

    # systemDelay as scalar
    body = json.dumps({"Distance": [dict(base_dist)], "SystemDelay": 200},
                      separators=(",", ":")).encode()
    p.send("Distance + SystemDelay:200 (scalar)", "SET_SETDAT", body)

    body = json.dumps({"Distance": [dict(base_dist)], "systemDelay": 200},
                      separators=(",", ":")).encode()
    p.send("Distance + systemDelay:200 (scalar)", "SET_SETDAT", body)

    # Try same fields ALONE (not combined)
    print("\n--- Delay-shaped fields alone (not combined with Distance) ---")
    body = json.dumps({"Delay": [{"FL": 100, "C": 100, "FR": 100,
                                    "SLA": 100, "SRA": 100, "TFL": 100,
                                    "TFR": 100, "TRL": 100, "TRR": 100,
                                    "SW1": 100}]},
                      separators=(",", ":")).encode()
    p.send("Delay alone [{ch:100,...}]", "SET_SETDAT", body)

    body = json.dumps({"DelayAdjustment": [{"FL": 100, "C": 100, "FR": 100,
                                            "SLA": 100, "SRA": 100, "TFL": 100,
                                            "TFR": 100, "TRL": 100, "TRR": 100,
                                            "SW1": 100}]},
                      separators=(",", ":")).encode()
    p.send("DelayAdjustment alone [{ch:100,...}]", "SET_SETDAT", body)

    body = json.dumps({"SystemDelay": 200}, separators=(",", ":")).encode()
    p.send("SystemDelay:200 alone", "SET_SETDAT", body)

    p.send("EXIT_AUDMD", "EXIT_AUDMD")
    p.close()


if __name__ == "__main__":
    main()
