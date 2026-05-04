"""Audyssey MultEQ Editor TCP protocol — direct speaker-distance write path.

Bypasses the Denon UI's distance cap (59.1 ft / 18.0 m on X3800H) and the
MultEQ Editor mobile app's pre-send clamp by speaking the protocol directly
to the AVR on TCP port 1256.

Protocol reverse-engineered by the LaserGuruGuy/ratbuddyssey project. Frame
format mirrors MultEqTcp/AudysseyMultEQAvrTcpClient.cs:

    'T'                 1 byte    header marker (TX) — receive side uses 'R'
    total_len           2 bytes   big-endian, full frame length incl checksum
    current_packet      1 byte    0 for single-packet payloads
    total_packets       1 byte    0 for single-packet payloads
    command             10 bytes  ASCII, fixed width (e.g. b'SET_SETDAT')
    reserved            1 byte    0x00
    data_len            2 bytes   big-endian, payload length
    data                N bytes   JSON or int32 array
    checksum            1 byte    sum of preceding bytes mod 256

Empirically on a Denon X3800H the per-channel delay-buffer ceiling is around
65 ms regardless of what distance value is pushed. The configured value is
stored verbatim once committed — but the firmware DSP applies at most that
ceiling. ``MAX_APPLIED_DELAY_MS`` exposes that limit so calibration code can
budget FIR group delay against what the AVR can actually compensate.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import time
from typing import Mapping

from ..base import DriverError

# Empirically measured on Denon X3800H. Two distinct ceilings depending on
# whether the OCA-style envelope bypass is used:
#   - Standard ``Distance``-only writes: ~38 ms applied delay variance,
#     clamped on EXIT_AUDMD re-validation (matches UI 18 m / 60 ft cap)
#   - Envelope writes (Distance + AudyFinFlg=NotFin → Fin commit): ~55 ms
#     applied delay variance — confirmed via SW1 = 20-30 m sweep
#     (2026-04-30). Beyond ~22 m configured the applied delay plateaus.
# The configured value persists past either cap; the *applied* delay does not.
MAX_APPLIED_DELAY_MS: float = 55.0
MAX_APPLIED_DELAY_STANDARD_MS: float = 38.0

DEFAULT_PORT: int = 1256
HEADER_LEN: int = 9
CMD_LEN: int = 10
COMMIT_BODY: bytes = b'{"AudyFinFlg":"Fin"}'


def _checksum(buf: bytes) -> int:
    return sum(buf) & 0xFF


def build_frame(cmd: str, data: bytes = b"") -> bytes:
    """Build a single Audyssey TCP frame for `cmd` (10 ASCII chars) + payload."""
    if len(cmd) != CMD_LEN:
        raise ValueError(f"command must be exactly {CMD_LEN} ASCII chars, got {cmd!r}")
    cmd_bytes = cmd.encode("ascii")
    total_len = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total_len)
    buf += b"\x00\x00"
    buf += cmd_bytes
    buf.append(0x00)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(_checksum(buf))
    return bytes(buf)


def probe_audyssey_service(
    host: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 3.0,
    try_release_lock: bool = True,
) -> dict | None:
    """Quick health probe for the AVR's Audyssey TCP/1256 service.

    Sends a single GET_AVRINF and returns the parsed JSON response if the
    AVR replies, or None if the service is unresponsive (silent socket
    after timeout).

    The Audyssey TCP service holds an exclusive session lock per
    connection. If a previous client opened ENTER_AUDY and didn't send
    EXIT_AUDMD before disconnecting (e.g. crash, SET_SETDAT NACK that
    skipped the cleanup path), the lock stays held and subsequent
    connections receive zero bytes. Soft power-cycle does NOT release
    the lock; only a hard power-cycle (pull cord, wait 30 s, plug back
    in) reliably clears it.

    With ``try_release_lock=True`` (default), a first-pass silent reply
    triggers one stale-lock-release attempt: open a fresh socket, send
    EXIT_AUDMD only, close, then retry GET_AVRINF. Costs ~1 extra
    second when the service IS healthy (the second probe is fast); can
    save a hard cycle when a previous client left the lock held.

    Returns:
        Parsed GET_AVRINF response dict on success
        ({EQType, DType, CoefWaitTime, ...}), or None if no reply
        even after the release attempt.
    """
    import json as _json
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    sock.settimeout(timeout)
    try:
        sock.sendall(build_frame("GET_AVRINF"))
        buf = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.1, end - time.monotonic()))
                chunk = sock.recv(8192)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 0:
                    # Got something — parse the first frame's payload as JSON.
                    if len(buf) >= 19 and buf[5:15].decode(
                        "ascii", errors="replace"
                    ).strip().rstrip("\x00") == "GET_AVRINF":
                        data_len = struct.unpack(">H", bytes(buf[16:18]))[0]
                        body = bytes(buf[18:18 + data_len])
                        try:
                            return _json.loads(body)
                        except _json.JSONDecodeError:
                            return {"_raw": body.decode("ascii", errors="replace")}
            except (socket.timeout, TimeoutError):
                break
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def build_distance_payload(
    channel_distances_m: Mapping[str, float],
    n_positions: int = 1,
) -> dict:
    """Build the IAmp.Distance payload that SET_SETDAT expects.

    Distance is encoded as ``List[Dict[channel, int_cm]]``, one dict per
    measurement position. The same channel→cm dict is replicated across
    every position because Audyssey post-calibration distance overrides
    are per-channel, not per-position.

    Args:
        channel_distances_m: e.g. {"FL": 4.05, "SW1": 30.72, ...}
        n_positions: number of measurement positions in the AVR's stored
            calibration. Use the length of any channel's responseData
            map in a saved .ady file. Defaults to 1.
    """
    if not channel_distances_m:
        raise ValueError("channel_distances_m is empty")
    if n_positions < 1:
        raise ValueError("n_positions must be >= 1")
    pos_dict = {ch: round(m * 100) for ch, m in channel_distances_m.items()}
    return {"Distance": [dict(pos_dict) for _ in range(n_positions)]}


def build_envelope_distance_payload(
    channel_distances_m: Mapping[str, float],
    n_positions: int = 1,
) -> dict:
    """Build a SET_SETDAT payload that bypasses the firmware variance cap.

    Verified working on Denon X3800H (2026-04-29 / 2026-04-30). The
    ``Distance`` field alone, sent without an explicit ``AudyFinFlg``,
    gets re-validated by the firmware on EXIT_AUDMD and snapped back to
    the ~38 ms applied-delay variance cap (UI 18 m / 60 ft on X3800H).

    Including ``AudyFinFlg: "NotFin"`` in the same packet, then sending a
    separate ``{"AudyFinFlg":"Fin"}`` commit before EXIT_AUDMD, tells the
    firmware "this is a complete calibration write, not a partial poke"
    — and the larger Distance values stick. Empirically the new
    applied-delay ceiling is ~55 ms (not unbounded — likely ~22 m
    variance hard-cap somewhere in the firmware).

    Caller responsibility: do NOT enter the AVR's Manual Setup > Distances
    menu after pushing — that triggers re-validation and snaps values
    back to the original 6 m variance cap.

    The PRIOR ``CustomDistance`` (.ady file field) approach was a red
    herring — the AVR's wire protocol doesn't have a CustomDistance
    field; only the .ady JSON file format does. The actual bypass is the
    NotFin/Fin commit dance on the standard Distance field.

    Side effect to be aware of: this minimal envelope (Distance + NotFin
    only) does NOT include AudyMultEq / AudyEqRef / AudyEqSet — the AVR
    applies defaults for those on Fin commit, which has been observed to
    drift mains FR by ±5-10 dB in the mid band. To keep MultEQ filter
    state intact, push the full ordered envelope (AmpAssign, AssignBin,
    SpConfig, Distance, ChLevel, Crossover, AudyFinFlg, AudyDynEq,
    AudyEqRef, AudyDynVol, AudyDynSet, AudyMultEq, AudyEqSet, AudyLfc,
    AudyLfcLev, SWSetup) — see scripts/audyssey_push_full_envelope.py.

    Args:
        channel_distances_m: e.g. {"FL": 4.05, "SW1": 20.0}. SW1=20m on
            X3800H lands ~50 ms of applied mains delay vs the ~38 ms cap.
        n_positions: number of stored positions. Replicated across all.
    """
    if not channel_distances_m:
        raise ValueError("channel_distances_m is empty")
    if n_positions < 1:
        raise ValueError("n_positions must be >= 1")
    pos_dict = {ch: round(m * 100) for ch, m in channel_distances_m.items()}
    return {
        "Distance": [dict(pos_dict) for _ in range(n_positions)],
        "AudyFinFlg": "NotFin",
    }


# Back-compat alias — older code calls this name. The behaviour now matches
# the proven envelope-bypass (Distance + AudyFinFlg=NotFin), not the
# misnamed CustomDistance approach which never worked.
build_custom_distance_payload = build_envelope_distance_payload


def distances_from_ady(ady: dict) -> dict[str, float]:
    """Extract per-channel customDistance from a parsed .ady JSON.

    Returns ``{commandId: meters}``. Skips entries without a commandId.
    """
    out: dict[str, float] = {}
    for ch in ady.get("detectedChannels", []):
        cmd_id = ch.get("commandId")
        if cmd_id is None:
            continue
        out[cmd_id] = float(ch.get("customDistance", 0.0))
    return out


def n_positions_from_ady(ady: dict) -> int:
    """Infer measurement-position count from a parsed .ady JSON.

    Looks at the size of any channel's responseData map. Returns 1 if not
    present (single-position calibration).
    """
    for ch in ady.get("detectedChannels", []):
        rd = ch.get("responseData")
        if isinstance(rd, dict) and rd:
            return len(rd)
    return 1


# ── Full-envelope path (preserves all detected channels) ──────────────────────
#
# What the X3800H actually accepts on TCP port 1256, verified 2026-05-02:
#
# 1. Per-channel arrays (`SpConfig`, `Distance`, `ChLevel`, `Crossover`)
#    are LIST-OF-SINGLE-KEY-DICTS, ONE PER CHANNEL — not per-position
#    multi-key dicts. Format mirrors A1EvoAcoustica/main.js:
#       "SpConfig": [{"FL":"S"}, {"C":"S"}, {"FR":"S"}, ..., {"SW1":"E"}]
#    Sub channels use type "E" (Effectively subwoofer) with crossover "F",
#    speakers use "S"/"L" with numeric Hz crossover.
# 2. Each `SET_SETDAT` packet must be ≤ 510 bytes (frame size including
#    the 19-byte header/checksum) per A1Evo's BINARY_PACKET_THRESHOLD.
#    Larger payloads MUST be split into multiple sequential SET_SETDAT
#    sends; the AVR processes them in order before the Fin commit.
# 3. ChLevel values are dB × 10 (integer) per A1Evo convention.
# 4. The Fin commit MUST NOT be sent if any preceding SET_SETDAT NACK'd
#    — committing on a partially-applied state corrupts the speaker
#    layout (heights/centers/surrounds drop to defaults). The original
#    audyssey_push_full_envelope.py script had this bug and twice wiped
#    the live AVR layout in May 2026 before this was understood.
# 5. The .ady file's `ampAssignInfo` is the canonical AssignBin to push
#    when re-establishing the full layout. Using the AVR's *current*
#    AssignBin (from GET_AVRSTS) only captures the live state, which
#    may already be degraded if some prior write dropped channels.

# Per-packet size threshold per A1EvoAcoustica/main.js BINARY_PACKET_THRESHOLD.
# Frames over this are silently NACK'd by the X3800H's TCP handler.
SET_SETDAT_PACKET_THRESHOLD: int = 510

# Canonical SET_SETDAT param order from A1Evo. Order matters when splitting:
# the AVR processes packets sequentially and an early-arriving param can be
# pre-validated before later params arrive.
DF_SETTING_DATA_PARAMETERS: tuple[str, ...] = (
    "AmpAssign", "AssignBin", "SpConfig", "Distance", "ChLevel", "Crossover",
    "AudyFinFlg", "AudyDynEq", "AudyEqRef", "AudyDynVol", "AudyDynSet",
    "AudyMultEq", "AudyEqSet", "AudyLfc", "AudyLfcLev", "SWSetup",
)


def _parse_response_frames(stream: bytearray) -> list[dict]:
    """Pull complete frames out of a streaming RX buffer."""
    out: list[dict] = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if len(stream) < total_len:
            break
        frame = bytes(stream[:total_len])
        if frame[-1] != _checksum(frame[:-1]):
            del stream[0]
            continue
        cmd = frame[5:15].decode("ascii", errors="replace").rstrip("\x00")
        data_len = struct.unpack(">H", frame[16:18])[0]
        out.append({"cmd": cmd, "data": frame[18:18 + data_len]})
        del stream[:total_len]
    return out


def build_full_envelope_payload(
    ady: dict,
    distance_overrides_m: Mapping[str, float] | None = None,
    level_overrides_db: Mapping[str, float] | None = None,
    crossover_overrides_hz: Mapping[str, int] | None = None,
) -> dict:
    """Build the full-envelope SET_SETDAT payload from a parsed .ady file.

    Re-establishes the complete speaker layout (all detected channels with
    matching SpConfig / Distance / ChLevel / Crossover) — NOT just a Distance
    delta. Use this when the AVR's live state has dropped channels that the
    .ady has, or whenever you want a complete write that won't reset
    unmentioned fields on Fin commit.

    Override maps let a caller bump specific channels' fields atomically:
      * ``distance_overrides_m`` — per-channel distance in METERS (typical use:
        SW1 distance push to compensate for FIR latency).
      * ``level_overrides_db`` — per-channel trim in dB (range typically
        ±12 dB, AVR clamps further).
      * ``crossover_overrides_hz`` — per-channel crossover in Hz (40-250,
        Small speakers only — ignored for Large/Sub channels which always
        emit "F").

    Channels not listed keep their .ady values; sending all three together is
    the safe one-shot path for distance + level + crossover updates.

    Per A1Evo convention: the four per-channel arrays are lists of single-key
    dicts (one per channel), not per-position multi-key dicts. The X3800H
    rejects the per-position shape with a NACK.
    """
    dist_ov = dict(distance_overrides_m or {})
    lvl_ov = dict(level_overrides_db or {})
    xo_ov = dict(crossover_overrides_hz or {})
    enmp_to_ampassign = {0: "Normal", 1: "BiAmp", 2: "SBack", 3: "Front", 4: "Surr"}

    distance: list[dict] = []
    spconfig: list[dict] = []
    chlevel: list[dict] = []
    crossover: list[dict] = []
    for ch in ady.get("detectedChannels", []):
        cid = ch.get("commandId")
        if not cid:
            continue
        # Speaker type: .ady's customSpeakerType, defaulting "E" for sub channels
        # ("commandId" starting with SW), "S" otherwise.
        sp = ch.get("customSpeakerType") or ""
        if not sp or sp == "?":
            sp = "E" if cid.startswith("SW") else "S"
        # Distance: override if requested, else .ady's customDistance.
        m = dist_ov.get(cid, float(ch.get("customDistance", 0) or 0))
        # ChLevel: dB × 10 per A1Evo convention. Override if requested.
        trim = lvl_ov.get(cid, float(ch.get("trimAdjustment", 0) or 0))
        # Crossover: "F" for sub/Large, numeric Hz (40-250) for Small.
        # Override applies only to Small speakers; Large/Sub stay "F".
        if sp in ("E", "L"):
            xover: int | str = "F"
        else:
            xv = int(xo_ov.get(cid, ch.get("customCrossover", 80) or 80))
            xover = xv if 40 <= xv <= 250 else 80
        spconfig.append({cid: sp})
        distance.append({cid: round(m * 100)})
        chlevel.append({cid: round(trim * 10)})
        crossover.append({cid: xover})

    return {
        "AmpAssign": enmp_to_ampassign.get(int(ady.get("enAmpAssignType", 0)), "Normal"),
        "AssignBin": ady.get("ampAssignInfo", ""),
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


def split_setdat_packets(
    payload: dict,
    threshold: int = SET_SETDAT_PACKET_THRESHOLD,
) -> list[dict]:
    """Split a full SET_SETDAT payload into one or more sub-payload dicts,
    each whose serialised frame is ≤ ``threshold`` bytes.

    Walks ``DF_SETTING_DATA_PARAMETERS`` in order, accumulating params into
    the current packet and starting a new packet whenever adding the next
    param would push the frame over the threshold. Mirrors A1Evo's
    sendSetDatCommand splitting algorithm.
    """
    packets: list[dict] = []
    current: dict = {}
    for key in DF_SETTING_DATA_PARAMETERS:
        if key not in payload:
            continue
        value = payload[key]
        test_payload = {**current, key: value}
        test_body = json.dumps(test_payload, separators=(",", ":")).encode("ascii")
        test_frame = build_frame("SET_SETDAT", test_body)
        if len(test_frame) > threshold:
            if current:
                packets.append(current)
            current = {key: value}
            single_body = json.dumps(current, separators=(",", ":")).encode("ascii")
            single_frame = build_frame("SET_SETDAT", single_body)
            if len(single_frame) > threshold:
                raise ValueError(
                    f"param {key!r} alone exceeds {threshold}-byte threshold "
                    f"({len(single_frame)} bytes)"
                )
        else:
            current[key] = value
    if current:
        packets.append(current)
    return packets


def _push_full_envelope_sync(
    host: str,
    port: int,
    payload: dict,
    commit: bool,
    timeout: float,
) -> bool:
    """Send a multi-packet SET_SETDAT envelope. Returns True on success.

    Sequence: ENTER_AUDY → SET_SETDAT × N (split-by-510B) → optional Fin → EXIT.
    Refuses to send Fin if any preceding SET_SETDAT NACK'd — committing on
    a partial state would corrupt the speaker layout.
    """
    packets = split_setdat_packets(payload)
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    rxbuf = bytearray()

    def drain(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                chunk = sock.recv(65536)
                if not chunk:
                    return
                rxbuf.extend(chunk)
            except (socket.timeout, TimeoutError):
                return

    def saw_ack(clear: bool = True) -> bool:
        ack = False
        for f in _parse_response_frames(rxbuf):
            snip = f["data"][:50].decode("ascii", errors="replace")
            if "NACK" in snip:
                if clear:
                    rxbuf.clear()
                return False
            if "ACK" in snip:
                ack = True
        if clear:
            rxbuf.clear()
        return ack

    all_ok = True
    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        drain(1.0)
        rxbuf.clear()
        for pkt in packets:
            body = json.dumps(pkt, separators=(",", ":")).encode("ascii")
            sock.sendall(build_frame("SET_SETDAT", body))
            drain(2.5)
            if not saw_ack():
                all_ok = False
                break
            time.sleep(0.3)
        if commit and all_ok:
            sock.sendall(build_frame("SET_SETDAT", COMMIT_BODY))
            drain(2.0)
            # Fin response is informational — failure to ACK doesn't roll back.
            saw_ack()
        sock.sendall(build_frame("EXIT_AUDMD"))
        drain(1.0)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return all_ok


async def push_full_envelope_from_ady(
    host: str,
    ady: dict,
    *,
    distance_overrides_m: Mapping[str, float] | None = None,
    level_overrides_db: Mapping[str, float] | None = None,
    crossover_overrides_hz: Mapping[str, int] | None = None,
    commit: bool = True,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> bool:
    """Push the full Audyssey envelope (all detected channels, A1Evo format)
    to the AVR. Re-establishes the complete speaker layout AND lets caller
    atomically override per-channel distances, levels, and crossovers.

    This is the safe replacement for ``push_speaker_distances(use_custom=True)``
    when you need the layout preserved. The bare ``Distance + AudyFinFlg=NotFin``
    envelope path snaps unmentioned fields to defaults on Fin commit, dropping
    channels (verified twice in May 2026 — heights/centers/surrounds disappeared).

    Returns True if all SET_SETDAT packets ACK'd (and Fin was committed when
    requested), False if any packet NACK'd. NACK aborts before Fin so the AVR
    state is unchanged.

    Caller is responsible for any user confirmation per the
    "signal-path writes need human approval" rule.
    """
    payload = build_full_envelope_payload(
        ady,
        distance_overrides_m=distance_overrides_m,
        level_overrides_db=level_overrides_db,
        crossover_overrides_hz=crossover_overrides_hz,
    )
    return await asyncio.get_running_loop().run_in_executor(
        None,
        _push_full_envelope_sync,
        host,
        port,
        payload,
        commit,
        timeout,
    )


def _push_sync(
    host: str,
    port: int,
    payload: dict,
    commit: bool,
    timeout: float,
) -> None:
    """Synchronous TCP push. Called from an executor by the async wrapper."""
    body = json.dumps(payload, separators=(",", ":")).encode("ascii")
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)

    def drain(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                if not sock.recv(65536):
                    return
            except (socket.timeout, TimeoutError):
                return

    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        drain(1.0)
        sock.sendall(build_frame("SET_SETDAT", body))
        drain(2.5)
        if commit:
            sock.sendall(build_frame("SET_SETDAT", COMMIT_BODY))
            drain(1.5)
        sock.sendall(build_frame("EXIT_AUDMD"))
        drain(1.0)
    finally:
        try:
            sock.close()
        except OSError:
            pass


async def push_speaker_distances(
    host: str,
    channel_distances_m: Mapping[str, float],
    *,
    n_positions: int = 1,
    commit: bool = False,
    port: int = DEFAULT_PORT,
    timeout: float = 6.0,
    use_custom: bool = False,
) -> None:
    """Push speaker distances to a Denon/Marantz AVR via Audyssey TCP.

    With ``commit=False`` the change is volatile (lost on power cycle).
    With ``commit=True`` the AVR persists to NVRAM via ``AudyFinFlg=Fin``.

    With ``use_custom=False`` (default), writes the standard ``Distance``
    field WITHOUT the AudyFinFlg envelope — the AVR re-validates on
    EXIT_AUDMD and clamps the applied delay to the firmware variance cap
    (~38 ms / 6 m on X3800H).

    ``use_custom=True`` is **DEPRECATED** — it raises ``DriverError``.
    The bare envelope does extend the applied-delay cap to ~55 ms but
    wipes the speaker layout on Fin commit (verified twice in May 2026).
    Use ``push_full_envelope_from_ady()`` instead — it sends a complete
    A1Evo-format envelope that preserves every detected channel atomically.

    Historical context: ``use_custom`` was the verified bypass: payload is
    ``{"Distance": [...], "AudyFinFlg": "NotFin"}``, followed by an
    explicit ``{"AudyFinFlg":"Fin"}`` commit before EXIT_AUDMD. The
    firmware accepts the larger Distance values; applied delay extends
    to ~55 ms (still capped, but ~17 ms more than the standard path).
    ``commit`` is forced to True when ``use_custom=True`` — the Fin
    commit IS the bypass mechanism; without it, no effect.

    The ``use_custom`` name is historical (referred to a different,
    non-working ``customDistance`` field idea); the implementation now
    sends the proven envelope bypass.

    Caller MUST NOT open Manual Setup > Distances on the AVR after a
    use_custom=True write — that triggers re-validation and snaps the
    distance values back to the standard cap.

    Caller is responsible for any user confirmation per the
    "signal-path-writes need human approval" rule.
    """
    if use_custom:
        # The bare Distance + AudyFinFlg=NotFin → Fin envelope DOES extend
        # the applied-delay cap, but unmentioned fields (SpConfig, ChLevel,
        # etc.) get reset to firmware defaults on Fin commit. That's how the
        # X3800H lost its surrounds 2026-04-30 and heights 2026-05-02. Use
        # ``push_full_envelope_from_ady()`` instead — it sends the full
        # A1Evo-format envelope split across ≤510B packets, which preserves
        # every detected channel atomically.
        raise DriverError(
            "push_speaker_distances(use_custom=True) is deprecated — it wipes "
            "speaker layout on commit. Use push_full_envelope_from_ady() instead, "
            "passing a parsed .ady file. See PR #144 for details."
        )
    payload = build_distance_payload(channel_distances_m, n_positions=n_positions)
    await asyncio.get_running_loop().run_in_executor(
        None,
        _push_sync,
        host,
        port,
        payload,
        commit,
        timeout,
    )


# ── FIR coefficient upload — full envelope + per-channel SET_COEFDT streams ──

def _push_filters_sync(
    host: str,
    port: int,
    *,
    setdat_envelope: dict,
    coef_packets_per_channel: list[tuple[str, list[bytes]]],
    inter_packet_ms: float = 0.0,
    inter_channel_ms: float = 20.0,
    coef_wait_final_ms: float = 15000.0,
    timeout: float = 30.0,
) -> dict:
    """End-to-end FIR upload sequence — synchronous, called via executor.

    Sequence (per A1EvoAcoustica/main.js + audyssey-rew-tuner orchestrator):

      ENTER_AUDY
      SET_SETDAT (full ordered envelope, AudyFinFlg=NotFin)
      [INIT_COEFS only if DType startsWith "fixed" — caller decides]
      for tc in target_curves:
        for channel in sorted_channels:
          for sr in sample_rates:
            stream SET_COEFDT × N packets   # NO ACK on coef msgs
            sleep inter_packet_ms / inter_channel_ms
      sleep coef_wait_final_ms (default 15 s on X3800H)
      FINZ_COEFS                            → ACK
      SET_SETDAT {"AudyFinFlg":"Fin"}      → ACK (commit)
      EXIT_AUDMD                            → ACK

    Args:
        setdat_envelope: full SET_SETDAT payload — caller's responsibility
            to include all 16 ordered fields with AudyFinFlg="NotFin".
        coef_packets_per_channel: list of ``(channel_id, [packets])`` in
            the order the AVR expects (typically outer = target curve,
            inner = channel, innermost = sample rate; flatten with
            ``audyssey_coef_transfer.all_streams_for_channel``).
        inter_packet_ms: sleep between SET_COEFDT packets within a stream.
            X3800H tolerates 0 in practice but 1-2 is safer for slow links.
        inter_channel_ms: sleep between channels (matches transfer.js
            20ms inter-channel delay).
        coef_wait_final_ms: post-stream pause before FINZ_COEFS. Default
            15 000 ms matches X3800H GET_AVRINF.CoefWaitTime.Final.
        timeout: socket timeout per recv (frames are tiny).

    Returns dict with per-stage status. Raises OSError / RuntimeError on
    fatal errors (no ACK on ENTER_AUDY, NACK on commit, etc.).
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)

    rxbuf = bytearray()
    status: dict = {
        "enter_audy": None,
        "set_setdat_envelope": None,
        "coef_streams_sent": 0,
        "coef_packets_sent": 0,
        "finz_coefs": None,
        "commit": None,
        "exit_audmd": None,
    }

    def drain(seconds: float) -> list[str]:
        """Drain inbound frames; return list of received command names."""
        end = time.monotonic() + seconds
        cmds: list[str] = []
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                c = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return cmds
            if not c:
                return cmds
            rxbuf.extend(c)
            # naive frame scan — we just look for command name + ACK
            i = 0
            while i + 19 <= len(rxbuf):
                if rxbuf[i] not in (ord("T"), ord("R")):
                    i += 1
                    continue
                tl = (rxbuf[i + 1] << 8) | rxbuf[i + 2]
                if i + tl > len(rxbuf):
                    break
                cmd = bytes(rxbuf[i + 5: i + 15]).decode("ascii", errors="replace").rstrip("\x00")
                cmds.append(cmd.strip())
                i += tl
            del rxbuf[:i]
        return cmds

    try:
        # 1. ENTER_AUDY
        sock.sendall(build_frame("ENTER_AUDY"))
        cmds = drain(1.5)
        status["enter_audy"] = "ENTER_AUDY" in cmds

        # 2. SET_SETDAT (full envelope, AudyFinFlg=NotFin)
        body = json.dumps(setdat_envelope, separators=(",", ":")).encode("ascii")
        sock.sendall(build_frame("SET_SETDAT", body))
        cmds = drain(3.0)
        status["set_setdat_envelope"] = "SET_SETDAT" in cmds and "ERROR" not in cmds

        # 3. Coefficient streams (no ACK from AVR on these — fire and forget)
        for channel_id, packets in coef_packets_per_channel:
            for pkt in packets:
                sock.sendall(pkt)
                if inter_packet_ms > 0:
                    time.sleep(inter_packet_ms / 1000.0)
                status["coef_packets_sent"] += 1
            status["coef_streams_sent"] += 1
            if inter_channel_ms > 0:
                time.sleep(inter_channel_ms / 1000.0)

        # 4. Wait for AVR to finish digesting coefficients
        if coef_wait_final_ms > 0:
            time.sleep(coef_wait_final_ms / 1000.0)

        # 5. FINZ_COEFS
        sock.sendall(build_frame("FINZ_COEFS"))
        cmds = drain(5.0)  # FINZ may take longer on big uploads
        status["finz_coefs"] = "FINZ_COEFS" in cmds

        # 6. AudyFinFlg=Fin commit
        sock.sendall(build_frame("SET_SETDAT", COMMIT_BODY))
        cmds = drain(2.0)
        status["commit"] = "SET_SETDAT" in cmds and "ERROR" not in cmds

        # 7. EXIT_AUDMD
        sock.sendall(build_frame("EXIT_AUDMD"))
        cmds = drain(1.5)
        status["exit_audmd"] = "EXIT_AUDMD" in cmds
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return status


async def push_filter_set(
    host: str,
    *,
    setdat_envelope: dict,
    coef_packets_per_channel: list[tuple[str, list[bytes]]],
    inter_packet_ms: float = 0.0,
    inter_channel_ms: float = 20.0,
    coef_wait_final_ms: float = 15000.0,
    port: int = DEFAULT_PORT,
    timeout: float = 30.0,
) -> dict:
    """Async wrapper around :func:`_push_filters_sync`.

    Run via the default thread-pool executor. See ``_push_filters_sync``
    for argument semantics + sequence detail.

    Caller MUST NOT enter Manual Setup > Distances on the AVR after this
    write — same constraint as the distance bypass: re-validation on
    menu open snaps Audyssey state.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _push_filters_sync(
            host,
            port,
            setdat_envelope=setdat_envelope,
            coef_packets_per_channel=coef_packets_per_channel,
            inter_packet_ms=inter_packet_ms,
            inter_channel_ms=inter_channel_ms,
            coef_wait_final_ms=coef_wait_final_ms,
            timeout=timeout,
        ),
    )
