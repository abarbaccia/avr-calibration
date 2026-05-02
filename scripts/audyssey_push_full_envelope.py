"""Push the A1Evo full envelope to the AVR (OCA-style), splitting into multiple
SET_SETDAT packets to honor the X3800H's per-packet 510-byte limit.

Replaces the prior single-packet implementation which NACK'd on ~835-byte
full-envelope payloads. Verified working on Denon AVR-X3800H 2026-05-02:
- Re-establishes the height channels (recovered TFL/TFR/TRL/TRR after the
  prior partial-envelope path wiped them).
- Pushes SW1 distance overrides for FIR-latency compensation.

Wire-protocol corrections vs the old implementation:
  - Per-channel arrays use single-key dicts (one entry per channel),
    NOT per-position multi-key dicts. The X3800H rejects the latter.
  - Speaker types use 'E' for subwoofer (not 'S').
  - Crossover uses 'F' for sub/Large (not numeric).
  - ChLevel is dB × 10 (not raw dB).
  - Calibration flags are typed correctly (bool/int, not strings).
  - Payload split at 510-byte threshold (A1Evo's BINARY_PACKET_THRESHOLD).
  - Fin commit is REFUSED if any preceding SET_SETDAT NACK'd — prevents
    the earlier corruption mode where committing on a NACK'd state caused
    the AVR to apply defaults for unmentioned fields.

Sequence:
  ENTER_AUDY
  SET_SETDAT(packet 1)  ← partial params, ≤ 510B
  SET_SETDAT(packet 2)
  ...
  SET_SETDAT(packet N)
  SET_SETDAT({"AudyFinFlg":"Fin"})  ← final commit (only if all N preceding ACK'd)
  EXIT_AUDMD

Usage:
    audyssey_push_full_envelope.py [SW1_METERS] [--commit]

The .ady file path is hardcoded to /home/pi/.avr-calibration/<latest>.ady on
the Pi for the calibration use case; adjust the path constant below for a
different setup.
"""
from __future__ import annotations
import json, socket, struct, time, sys

HEADER_LEN, CMD_LEN = 9, 10
PACKET_THRESHOLD = 510  # A1Evo BINARY_PACKET_THRESHOLD

# A1Evo's canonical param order
DF_SETTING_DATA_PARAMETERS = [
    "AmpAssign", "AssignBin", "SpConfig", "Distance", "ChLevel", "Crossover",
    "AudyFinFlg", "AudyDynEq", "AudyEqRef", "AudyDynVol", "AudyDynSet",
    "AudyMultEq", "AudyEqSet", "AudyLfc", "AudyLfcLev", "SWSetup",
]

def build_frame(cmd, data=b""):
    cb = cmd.encode("ascii")
    total = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T")); buf += struct.pack(">H", total); buf += b"\x00\x00"
    buf += cb; buf.append(0x00); buf += struct.pack(">H", len(data)); buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)

def parse_frames(stream):
    out = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]; continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if len(stream) < total_len: break
        frame = bytes(stream[:total_len])
        if frame[-1] != (sum(frame[:-1]) & 0xFF):
            del stream[0]; continue
        cmd = frame[5:15].decode("ascii", errors="replace").rstrip("\x00")
        data_len = struct.unpack(">H", frame[16:18])[0]
        out.append({"cmd": cmd, "data": frame[18:18+data_len]})
        del stream[:total_len]
    return out

def build_params(ady, sw1_override_m=None):
    """Build the {key: value} param dict in canonical order."""
    ENMP_TO_AMPASSIGN = {0: "Normal", 1: "BiAmp", 2: "SBack", 3: "Front", 4: "Surr"}
    amp_assign = ENMP_TO_AMPASSIGN.get(int(ady.get("enAmpAssignType", 0)), "Normal")
    assign_bin = ady["ampAssignInfo"]

    distance, spconfig, chlevel, crossover = [], [], [], []
    for c in ady["detectedChannels"]:
        cid = c.get("commandId")
        if not cid:
            continue
        sp = c.get("customSpeakerType") or ""
        if not sp or sp == "?":
            sp = "E" if cid.startswith("SW") else "S"
        m = sw1_override_m if (cid == "SW1" and sw1_override_m is not None) \
            else float(c.get("customDistance", 0) or 0)
        trim = float(c.get("trimAdjustment", 0) or 0)
        if sp in ("E", "L"):
            xover = "F"
        else:
            xv = int(c.get("customCrossover", 80) or 80)
            xover = xv if 40 <= xv <= 250 else 80
        spconfig.append({cid: sp})
        distance.append({cid: round(m * 100)})
        chlevel.append({cid: round(trim * 10)})
        crossover.append({cid: xover})

    return {
        "AmpAssign": amp_assign,
        "AssignBin": assign_bin,
        "SpConfig": spconfig,
        "Distance": distance,
        "ChLevel": chlevel,
        "Crossover": crossover,
        "AudyFinFlg": "NotFin",
        "AudyDynEq": False,
        "AudyEqRef": 0,
        "AudyDynVol": False,
        "AudyDynSet": "L",
        "AudyMultEq": True,
        "AudyEqSet": "Flat",
        "AudyLfc": False,
        "AudyLfcLev": 3,
        "SWSetup": {"SWNum": 1, "SWMode": "N/A", "SWLayout": "N/A"},
    }

def split_params(params):
    """Pack params into a sequence of dicts, each whose JSON+frame ≤ PACKET_THRESHOLD.
    Mirrors A1Evo's sendSetDatCommand splitting algorithm."""
    packets = []
    current = {}
    for key in DF_SETTING_DATA_PARAMETERS:
        if key not in params:
            continue
        value = params[key]
        test_payload = {**current, key: value}
        test_body = json.dumps(test_payload, separators=(",", ":")).encode("ascii")
        test_frame = build_frame("SET_SETDAT", test_body)
        if len(test_frame) > PACKET_THRESHOLD:
            if current:
                packets.append(current)
            current = {key: value}
            single_body = json.dumps(current, separators=(",", ":")).encode("ascii")
            single_frame = build_frame("SET_SETDAT", single_body)
            if len(single_frame) > PACKET_THRESHOLD:
                raise ValueError(f"Param {key} alone exceeds {PACKET_THRESHOLD}B")
        else:
            current[key] = value
    if current:
        packets.append(current)
    return packets

def push(host, params, commit, port=1256, timeout=10.0):
    packets = split_params(params)
    print(f"  Split into {len(packets)} packet(s)")
    for i, p in enumerate(packets):
        body = json.dumps(p, separators=(",", ":")).encode("ascii")
        frame = build_frame("SET_SETDAT", body)
        keys = list(p.keys())
        print(f"    Pkt {i+1}: keys={keys} body={len(body)}B frame={len(frame)}B")

    s = socket.create_connection((host, port), timeout=timeout); s.settimeout(timeout)
    rx = bytearray()
    def drain(secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            try:
                s.settimeout(max(0.05, end - time.monotonic())); c = s.recv(65536)
            except (socket.timeout, TimeoutError): return
            if not c: return
            rx.extend(c)

    all_ok = True
    try:
        print("\n  [tx] ENTER_AUDY")
        s.sendall(build_frame("ENTER_AUDY")); drain(1.0); rx.clear()
        for i, p in enumerate(packets):
            body = json.dumps(p, separators=(",", ":")).encode("ascii")
            print(f"  [tx] SET_SETDAT pkt {i+1}/{len(packets)} ({len(body)}B)")
            s.sendall(build_frame("SET_SETDAT", body)); drain(2.5)
            this_ok = False
            for f in parse_frames(rx):
                snip = f["data"][:80].decode("ascii", errors="replace")
                print(f"    [rx] {f['cmd'].strip()} {len(f['data'])}B: {snip!r}")
                if "ACK" in snip and "NACK" not in snip:
                    this_ok = True
                if "NACK" in snip:
                    this_ok = False; break
            rx.clear()
            if not this_ok:
                all_ok = False
                print(f"  ✗ Packet {i+1} NACK'd — aborting before Fin")
                break
            time.sleep(0.3)
        if commit:
            if not all_ok:
                print("  ✗ REFUSING to send Fin (some packet NACK'd)")
            else:
                print("  [tx] SET_SETDAT (Fin commit)")
                s.sendall(build_frame("SET_SETDAT", b'{"AudyFinFlg":"Fin"}')); drain(3.0)
                for f in parse_frames(rx):
                    snip = f["data"][:80].decode("ascii", errors="replace")
                    print(f"    [rx] {f['cmd'].strip()} {len(f['data'])}B: {snip!r}")
        print("  [tx] EXIT_AUDMD")
        s.sendall(build_frame("EXIT_AUDMD")); drain(1.0)
    finally:
        s.close()
    return all_ok

if __name__ == "__main__":
    HOST = "192.168.1.209"
    sw1_m = float(sys.argv[1]) if len(sys.argv) > 1 else 17.91
    do_commit = "--commit" in sys.argv

    with open("/home/pi/.avr-calibration/1_BACKUP_20260427-160223.ady") as f:
        ady = json.load(f)

    params = build_params(ady, sw1_override_m=sw1_m)
    print(f"=== Params built from .ady ===")
    print(f"  AmpAssign: {params['AmpAssign']}")
    print(f"  AssignBin: {params['AssignBin'][:40]}...")
    print(f"  Channels: {[list(d.keys())[0] for d in params['SpConfig']]}")
    print(f"  SW1 distance: {[d['SW1'] for d in params['Distance'] if 'SW1' in d]} cm (= {sw1_m} m)")
    print()
    print(f"=== Pushing (commit={do_commit}) ===")
    ok = push(HOST, params, commit=do_commit)
    print()
    print("  RESULT: " + ("PUSH SUCCEEDED" if ok else "PUSH FAILED"))
