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

    With ``use_custom=True``, uses the verified bypass: payload is
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
        payload = build_envelope_distance_payload(
            channel_distances_m, n_positions=n_positions
        )
        commit = True  # NotFin envelope requires Fin commit to bypass the cap
    else:
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
