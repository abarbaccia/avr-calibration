"""Push a full Audyssey envelope to the AVR (OCA-style).

Mirrors A1EvoAcoustica/main.js upload sequence:
    ENTER_AUDY → SET_SETDAT(full ordered params, AudyFinFlg=NotFin)
              → SET_SETDAT({"AudyFinFlg":"Fin"}) → EXIT_AUDMD

Bypasses the firmware variance cap (~38 ms) that our partial-Distance-only
payload triggers on re-validation. The .ady provides per-channel values
(crossover, level, type, etc.); GET_AVRSTS provides AmpAssign+AssignBin.

Usage:
    audyssey_push_full_envelope.py SRC_ADY HOST [--override CH METERS]... [--commit]
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time

HEADER_LEN = 9
CMD_LEN = 10


def build_frame(cmd: str, data: bytes = b"") -> bytes:
    cb = cmd.encode("ascii")
    if len(cb) != CMD_LEN:
        raise ValueError(f"command must be {CMD_LEN} chars, got {cmd!r}")
    total = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total)
    buf += b"\x00\x00"
    buf += cb
    buf.append(0x00)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream: bytearray) -> list[dict]:
    out = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if len(stream) < total_len:
            break
        frame = bytes(stream[:total_len])
        if frame[-1] != (sum(frame[:-1]) & 0xFF):
            del stream[0]
            continue
        cmd = frame[5:15].decode("ascii", errors="replace").rstrip("\x00")
        data_len = struct.unpack(">H", frame[16:18])[0]
        out.append({"cmd": cmd, "data": frame[18:18 + data_len]})
        del stream[:total_len]
    return out


def get_avr_state(host: str, port: int = 1256, timeout: float = 6.0) -> dict:
    """ENTER_AUDY + GET_AVRSTS → returns {AmpAssign, AssignBin, ChSetup, SWSetup}."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    rxbuf = bytearray()
    state: dict = {}

    def drain(seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                c = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            if not c:
                return
            rxbuf.extend(c)
            for f in parse_frames(rxbuf):
                if f["cmd"].strip() == "GET_AVRSTS" and len(f["data"]) > 50:
                    try:
                        state.update(json.loads(f["data"]))
                    except Exception:
                        pass

    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        drain(1.0)
        sock.sendall(build_frame("GET_AVRSTS"))
        drain(2.0)
        sock.sendall(build_frame("EXIT_AUDMD"))
        drain(1.0)
    finally:
        sock.close()
    return state


def build_full_payload(
    ady: dict,
    avr_state: dict,
    distance_overrides_m: dict[str, float],
) -> dict:
    detected = ady["detectedChannels"]
    cmd_ids = [c["commandId"] for c in detected]

    # Per-channel maps. Distance override falls back to ady customDistance.
    distance_cm: dict[str, int] = {}
    chlevel: dict[str, float] = {}
    crossover: dict[str, int] = {}
    spconfig: dict[str, str] = {}
    for c in detected:
        cid = c["commandId"]
        m = distance_overrides_m.get(cid, float(c.get("customDistance", 0.0)))
        distance_cm[cid] = round(m * 100)
        chlevel[cid] = float(c.get("trimAdjustment", 0.0))
        crossover[cid] = int(c.get("customCrossover", 80))
        spconfig[cid] = c.get("customSpeakerType", "S")

    # n_pos = max len of responseData across channels
    n_pos = 1
    for c in detected:
        rd = c.get("responseData")
        if isinstance(rd, dict):
            n_pos = max(n_pos, len(rd))

    # AudyDynEq / AudyDynVol from .ady booleans → string enums
    dyneq = "On" if ady.get("dynamicEq") else "Off"
    dynvol = "Off" if not ady.get("dynamicVolume") else "Mid"

    # AmpAssign: enAmpAssignType=0 → "Normal"
    amp_assign_map = {0: "Normal", 1: "BiAmp", 2: "SBack", 3: "Front", 4: "Surr"}
    amp_assign = avr_state.get("AmpAssign") or amp_assign_map.get(int(ady.get("enAmpAssignType", 0)), "Normal")

    payload = {
        "AmpAssign": amp_assign,
        "AssignBin": avr_state.get("AssignBin") or ady.get("ampAssignInfo"),
        "SpConfig": [dict(spconfig) for _ in range(n_pos)],
        "Distance": [dict(distance_cm) for _ in range(n_pos)],
        "ChLevel": [dict(chlevel) for _ in range(n_pos)],
        "Crossover": [dict(crossover) for _ in range(n_pos)],
        "AudyFinFlg": "NotFin",
        "AudyDynEq": dyneq,
        "AudyEqRef": "0",
        "AudyDynVol": dynvol,
        "AudyDynSet": "Mid",
        "AudyMultEq": "Reference",
        "AudyEqSet": "Reference",
        "AudyLfc": "On" if ady.get("lfc") else "Off",
        "AudyLfcLev": 3,
        "SWSetup": avr_state.get("SWSetup", {"SWNum": 1, "SWMode": "N/A", "SWLayout": "N/A"}),
    }
    return payload


def push(host: str, port: int, payload: dict, commit: bool, timeout: float = 8.0):
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    rxbuf = bytearray()

    def drain(seconds: float, label: str = ""):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                c = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            if not c:
                return
            rxbuf.extend(c)
            for f in parse_frames(rxbuf):
                snippet = f["data"][:120].decode("ascii", errors="replace")
                print(f"  [rx{label}] {f['cmd'].strip()} {len(f['data'])}B: {snippet!r}")

    try:
        print("[tx] ENTER_AUDY")
        sock.sendall(build_frame("ENTER_AUDY"))
        drain(1.0)

        body = json.dumps(payload, separators=(",", ":")).encode("ascii")
        print(f"[tx] SET_SETDAT (full envelope, {len(body)} bytes)")
        sock.sendall(build_frame("SET_SETDAT", body))
        drain(3.0)

        if commit:
            commit_body = b'{"AudyFinFlg":"Fin"}'
            print(f"[tx] SET_SETDAT (commit) {commit_body!r}")
            sock.sendall(build_frame("SET_SETDAT", commit_body))
            drain(2.0)

        print("[tx] EXIT_AUDMD")
        sock.sendall(build_frame("EXIT_AUDMD"))
        drain(1.0)
    finally:
        sock.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="source .ady file")
    ap.add_argument("host", help="AVR IP")
    ap.add_argument("--port", type=int, default=1256)
    ap.add_argument(
        "--override", nargs=2, action="append", default=[],
        metavar=("CH", "METERS"),
        help="override one channel's distance (repeatable, e.g. --override SW1 30.0 --override FL 4.0)",
    )
    ap.add_argument("--commit", action="store_true", help="send AudyFinFlg=Fin")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.src) as f:
        ady = json.load(f)

    overrides_m = {ch: float(m) for ch, m in args.override}

    print(f"=== Querying AVR state at {args.host} ===")
    avr_state = get_avr_state(args.host, args.port)
    print(f"AmpAssign={avr_state.get('AmpAssign')!r} "
          f"AssignBin={avr_state.get('AssignBin', '')[:40]!r}... "
          f"SWSetup={avr_state.get('SWSetup')}")

    payload = build_full_payload(ady, avr_state, overrides_m)
    print(f"\n=== Full envelope payload ({len(json.dumps(payload))} bytes) ===")
    print(json.dumps(payload, indent=2)[:2000])

    if args.dry_run:
        print("\n--dry-run, not pushing.")
        return 0

    print(f"\n=== Pushing to {args.host} (commit={args.commit}) ===")
    push(args.host, args.port, payload, commit=args.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
