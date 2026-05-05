"""Full Audyssey upload orchestration: SET_SETDAT envelope + SET_COEFDT
coefficient streams + AudyFinFlg=Fin commit.

This is the layer that turns "I have FIR coefficients per channel" into
"the AVR is now running those coefficients in place of its stock
Audyssey calibration."

Wire-protocol sequence (TCP/1256, port from audyssey_tcp):

    ENTER_AUDY                                          → ACK
    SET_SETDAT (1..N chunks, ordered params,            → ACK each
                AudyFinFlg=NotFin in the first chunk)
    [INIT_COEFS  — only if DType startsWith "fixed"]    → ACK
    for each channel:
        for each (target_curve, sample_rate):
            SET_COEFDT × N packets (no ACK, fire-and-forget)
            sleep CoefWaitTime.Init
        sleep ~20ms
    sleep CoefWaitTime.Final  (~15 s on X3800H)
    FINZ_COEFS                                          → ACK
    SET_SETDAT {"AudyFinFlg":"Fin"}                     → ACK   ← commits
    EXIT_AUDMD                                          → ACK

Crucial behaviours discovered while reverse-engineering this protocol:

  1. SET_SETDAT chunks at 510 bytes per packet. The full 16-field envelope
     for a typical X3800H setup is 1.5-2 kB — it MUST be chunked or the
     AVR NACKs the entire write.

  2. Field types matter — the AVR rejects mistyped fields silently. Booleans
     stay booleans; integers stay integers; the `AudyMultEQ` field name has
     a capital `Q` at the end (`AudyMultEQ`, not `AudyMultEq`).

  3. Field order: AmpAssign, AssignBin, SpConfig, Distance, ChLevel,
     Crossover, AudyFinFlg, AudyDynEq, AudyEqRef, AudyDynVol, AudyDynSet,
     AudyMultEQ, AudyEqSet, AudyLfc, AudyLfcLev, SWSetup.

  4. The AudyFinFlg=Fin commit at the end IS required — without it the
     firmware re-validates Distance on EXIT_AUDMD and snaps it back to
     the variance cap. (See project_audyssey_envelope_bypass.md for the
     2026-04-30 SW1=20m verification that nailed this down.)

  5. Caller MUST NOT enter Manual Setup > Distances on the AVR after a
     successful upload — that triggers re-validation.

Sources cribbed from `srinivas486/audyssey-rew-tuner` (MIT-licensed):
  - oca_transfer.py:1146-1339  build_set_dat_params + send_set_dat_command
  - oca_transfer.py:680-791    build_avr_packet + generate_coef_packets
  - oca_transfer.py:1453-1620  upload orchestrator
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
import time
from typing import Iterable, Mapping, Sequence

from calibrate.audyssey_fir import is_sub_channel
from calibrate.drivers.denon.audyssey_coef_transfer import (
    XT32_SAMPLE_RATES_HZ,
    all_streams_for_channel,
)
from calibrate.drivers.denon.audyssey_tcp import (
    DEFAULT_PORT,
    build_frame,
)


def parse_frames(stream: bytearray) -> list[dict]:
    """Pull complete Audyssey TCP frames out of ``stream`` (consumes them).

    Returns a list of ``{"cmd": str, "data": bytes}`` dicts. Frames whose
    checksum doesn't validate are skipped (one byte at a time) until a
    valid frame is found.
    """
    out: list[dict] = []
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


SET_SETDAT_CHUNK_THRESHOLD_BYTES = 510

# Default Audyssey runtime calibration settings — these match what
# A1Evo Acoustica uses post-calibration. Override individually via the
# ``calibration_settings`` arg to ``build_set_dat_envelope`` if needed.
DEFAULT_CALIBRATION_SETTINGS: dict[str, object] = {
    "AudyFinFlg": "NotFin",
    "AudyDynEq": False,
    "AudyEqRef": 0,
    "AudyDynVol": False,
    "AudyDynSet": "L",
    "AudyMultEQ": True,   # NOTE the capital Q
    "AudyEqSet": "Flat",
    "AudyLfc": False,
    "AudyLfcLev": 3,
}

# Field order required by the AVR firmware. Unknown/None values are dropped.
SETDAT_PARAM_ORDER: tuple[str, ...] = (
    "AmpAssign",
    "AssignBin",
    "SpConfig",
    "Distance",
    "ChLevel",
    "Crossover",
    "AudyFinFlg",
    "AudyDynEq",
    "AudyEqRef",
    "AudyDynVol",
    "AudyDynSet",
    "AudyMultEQ",
    "AudyEqSet",
    "AudyLfc",
    "AudyLfcLev",
    "SWSetup",
)


def _build_setdat_frame(json_payload: str) -> bytes:
    """Build a single SET_SETDAT frame around a JSON body."""
    return build_frame("SET_SETDAT", json_payload.encode("ascii"))


def envelope_packet_size(payload: dict) -> int:
    """Return the wire-frame size for a SET_SETDAT packet carrying ``payload``."""
    body = json.dumps(payload, separators=(",", ":"))
    return len(_build_setdat_frame(body))


def chunk_setdat_payload(
    ordered_params: Sequence[tuple[str, object]],
    max_packet_bytes: int = SET_SETDAT_CHUNK_THRESHOLD_BYTES,
) -> list[dict]:
    """Split an ordered (key, value) sequence into the smallest list of
    JSON-serialisable dicts whose serialised SET_SETDAT frames each fit
    under ``max_packet_bytes``.

    Preserves the original field order — the AVR's parser requires it.
    Raises if any single parameter blows the threshold on its own.
    """
    chunks: list[dict] = []
    current: dict = {}

    for key, value in ordered_params:
        if value is None:
            continue
        candidate = dict(current)
        candidate[key] = value
        if envelope_packet_size(candidate) > max_packet_bytes:
            if current:
                chunks.append(current)
                current = {}
            single = {key: value}
            if envelope_packet_size(single) > max_packet_bytes:
                raise ValueError(
                    f"SET_SETDAT param {key!r} alone exceeds "
                    f"{max_packet_bytes}-byte threshold"
                )
            current = single
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def build_set_dat_envelope(
    ady: dict,
    avr_status: dict,
    *,
    distances_override_m: Mapping[str, float] | None = None,
    calibration_settings: Mapping[str, object] | None = None,
) -> list[tuple[str, object]]:
    """Build the ordered (key, value) param list for a SET_SETDAT envelope.

    Args:
        ady: parsed .ady JSON. Provides per-channel customDistance,
            customCrossover, customSpeakerType, trimAdjustment.
        avr_status: GET_AVRSTS response dict — provides AmpAssign,
            AssignBin, SWSetup. These cannot be derived from .ady alone.
        distances_override_m: optional ``{channel_id: meters}`` map. Any
            channel listed here overrides the .ady customDistance value.
            Use this to push e.g. SW1=20m for the variance-cap bypass.
        calibration_settings: override one or more of the AudyDynEq /
            AudyEqRef / etc. defaults. Field names + types must match
            DEFAULT_CALIBRATION_SETTINGS.

    Returns:
        Ordered list of (key, value) tuples — feed to ``chunk_setdat_payload``.
    """
    detected = ady.get("detectedChannels", [])
    if not detected:
        raise ValueError(".ady has no detectedChannels — cannot build envelope")

    overrides = dict(distances_override_m or {})

    # n_pos = max length of any channel's responseData dict
    n_pos = 1
    for c in detected:
        rd = c.get("responseData")
        if isinstance(rd, dict) and rd:
            n_pos = max(n_pos, len(rd))

    # Per-channel maps. Distance can be overridden; the rest come from .ady.
    sp_config: dict[str, str] = {}
    distance_cm: dict[str, int] = {}
    ch_level_dbx10: dict[str, int] = {}
    crossover: dict[str, object] = {}
    for c in detected:
        cid = c["commandId"]
        sp_config[cid] = c.get("customSpeakerType", "S")

        m = float(overrides.get(cid, c.get("customDistance", 0.0)))
        distance_cm[cid] = round(m * 100)

        trim_db = float(c.get("trimAdjustment", 0.0))
        ch_level_dbx10[cid] = int(trim_db * 10)

        speaker_type = sp_config[cid]
        # Subwoofer ("E") and Large ("L") get "F" crossover; everything else
        # uses customCrossover (Hz integer). Match A1Evo's convention.
        if speaker_type in ("E", "L"):
            crossover[cid] = "F"
        else:
            xover = c.get("customCrossover", 80)
            crossover[cid] = int(xover) if not isinstance(xover, str) else xover

    sw_setup = avr_status.get("SWSetup")
    if isinstance(sw_setup, dict) and sw_setup.get("SWNum") is not None:
        try:
            sw_num = int(sw_setup["SWNum"])
        except (TypeError, ValueError):
            sw_num = 0
        sw_setup_value = (
            {"SWNum": sw_num, "SWMode": "Standard", "SWLayout": "N/A"}
            if sw_num > 0
            else None
        )
    else:
        sw_setup_value = None

    settings = dict(DEFAULT_CALIBRATION_SETTINGS)
    if calibration_settings:
        settings.update(calibration_settings)

    values: dict[str, object | None] = {
        "AmpAssign": avr_status.get("AmpAssign"),
        "AssignBin": avr_status.get("AssignBin") or ady.get("ampAssignInfo"),
        "SpConfig": [dict(sp_config) for _ in range(n_pos)] if sp_config else None,
        "Distance": [dict(distance_cm) for _ in range(n_pos)] if distance_cm else None,
        "ChLevel": [dict(ch_level_dbx10) for _ in range(n_pos)] if ch_level_dbx10 else None,
        "Crossover": [dict(crossover) for _ in range(n_pos)] if crossover else None,
        "AudyFinFlg": settings["AudyFinFlg"],
        "AudyDynEq": settings["AudyDynEq"],
        "AudyEqRef": settings["AudyEqRef"],
        "AudyDynVol": settings["AudyDynVol"],
        "AudyDynSet": settings["AudyDynSet"],
        "AudyMultEQ": settings["AudyMultEQ"],
        "AudyEqSet": settings["AudyEqSet"],
        "AudyLfc": settings["AudyLfc"],
        "AudyLfcLev": settings["AudyLfcLev"],
        "SWSetup": sw_setup_value,
    }

    return [(k, values[k]) for k in SETDAT_PARAM_ORDER if values[k] is not None]


def query_avr_status(
    host: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 6.0,
) -> dict:
    """Open a transient TCP/1256 connection, run ENTER_AUDY +
    GET_AVRSTS + GET_AVRINF, return the parsed responses merged.

    Returned dict has keys including ``AmpAssign``, ``AssignBin``,
    ``ChSetup``, ``SWSetup`` (from GET_AVRSTS) and ``EQType``,
    ``DType``, ``CoefWaitTime`` (from GET_AVRINF).

    Raises ConnectionError if the Audyssey TCP service is wedged. The
    AVR's Audyssey daemon occasionally stops responding after a soft
    power-on from standby — HTTP / denonavr keep working, but every
    SET_SETDAT / SET_COEFDT / GET_AVR* command silently returns no data.
    Recovery: pull the AVR's power cord, wait 30 s, plug back in. Soft
    power-cycle (denonavr.async_power_on) does NOT fix this.
    """
    from . import audyssey_tcp
    if audyssey_tcp.probe_audyssey_service(host, port=port, timeout=3.0) is None:
        raise ConnectionError(
            f"Audyssey TCP service on {host}:{port} is unresponsive. "
            "AVR's Audyssey daemon is wedged — hard power-cycle the AVR "
            "(pull the power cord, wait 30 s, plug back in)."
        )

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    rxbuf = bytearray()
    state: dict = {}

    def drain(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            if not chunk:
                return
            rxbuf.extend(chunk)
            for f in parse_frames(rxbuf):
                cmd = f["cmd"].strip()
                data = f["data"]
                if not cmd or len(data) < 20:
                    continue
                try:
                    state.update(json.loads(data))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        drain(1.0)
        sock.sendall(build_frame("GET_AVRINF"))
        drain(1.5)
        sock.sendall(build_frame("GET_AVRSTS"))
        drain(2.0)
        sock.sendall(build_frame("EXIT_AUDMD"))
        drain(1.0)
    finally:
        sock.close()
    return state


def _push_full_sync(
    host: str,
    port: int,
    setdat_chunks: list[dict],
    coef_packet_streams: list[bytes],
    init_coefs_required: bool,
    coef_wait_init_ms: float,
    coef_wait_final_ms: float,
    inter_packet_delay_ms: float,
    timeout: float,
    commit_fin: bool = True,
    abort_fin_on_nack: bool = True,
    pre_coef_settle_ms: float = 500.0,
    setdisfil_bodies: list[dict] | None = None,
) -> dict:
    """Synchronous TCP push of the full upload sequence. Returns a dict
    summarising acks received per stage; raises on hard TCP errors.

    ``commit_fin`` — when False, skip the AudyFinFlg=Fin commit packet.
    The AVR's persisted Audyssey state is unchanged after EXIT_AUDMD.
    Use this for non-destructive multi-channel verification.

    ``abort_fin_on_nack`` — when True (default), if any NACK frames
    were observed during the SET_COEFDT stream, refuse to send the
    Fin commit. Committing on top of a partially-corrupted coefficient
    bank is the documented X3800H ChSetup-wipe trigger.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    rxbuf = bytearray()
    summary: dict = {
        "enter_audy_ack": False,
        "setdat_acks": [],
        "setdisfil_acks": [],
        "init_coefs_ack": None,
        "coef_packets_sent": 0,
        "coef_nack_count": 0,
        "coef_nack_per_channel": [],
        "finz_coefs_ack": False,
        "fin_commit_attempted": False,
        "fin_commit_ack": False,
        "exit_audmd_ack": False,
    }

    def drain(seconds: float) -> list[dict]:
        end = time.monotonic() + seconds
        frames: list[dict] = []
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            rxbuf.extend(chunk)
            new = parse_frames(rxbuf)
            for f in new:
                frames.append({"cmd": f["cmd"].strip(), "data": f["data"]})
        return frames

    def did_ack(frames: list[dict], cmd_name: str) -> bool:
        for f in frames:
            if f["cmd"] != cmd_name:
                continue
            try:
                if json.loads(f["data"]).get("Comm") == "ACK":
                    return True
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return False

    def count_nacks(frames: list[dict]) -> int:
        n = 0
        for f in frames:
            try:
                if json.loads(f["data"]).get("Comm") == "NACK":
                    n += 1
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return n

    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        summary["enter_audy_ack"] = did_ack(drain(1.0), "ENTER_AUDY")

        # SET_SETDAT envelope chunks
        for chunk in setdat_chunks:
            body = json.dumps(chunk, separators=(",", ":")).encode("ascii")
            sock.sendall(build_frame("SET_SETDAT", body))
            ack = did_ack(drain(2.5), "SET_SETDAT")
            summary["setdat_acks"].append(ack)
            if not ack:
                # NACK on a chunk = bail rather than push partial state.
                summary["error"] = "SET_SETDAT NACK"
                return summary

        # NOTE: SET_DISFIL is NOT used by OCA / A1Evo Acoustica
        # (transfer.js / oca_transfer.py don't send it). We briefly
        # added it 2026-05-04 evening based on the deleted
        # scripts/audyssey_push_full_filters.py — that script is a
        # different (probably non-working) port. Removed after finding
        # OCA's reference implementation.

        # INIT_COEFS — sent only when DType=fixed per OCA
        # (oca_transfer.py:1476). X3800H reports DType=Float so this
        # is normally skipped.
        if init_coefs_required:
            time.sleep(0.02)
            sock.sendall(build_frame("INIT_COEFS"))
            summary["init_coefs_ack"] = did_ack(drain(1.5), "INIT_COEFS")

        # Pre-coef settle. Run 29's verification probe (commit_fin=False)
        # caught 3 NACKs in the first channel's stream and zero in every
        # subsequent boundary — startup transient, not per-channel. Give
        # the AVR's DSP time to fully transition into SET_COEFDT-receive
        # state, and drain any pending frames the envelope phase may have
        # queued (X3800H sometimes emits status frames after SET_SETDAT
        # acks).
        if pre_coef_settle_ms > 0:
            pre_frames = drain(pre_coef_settle_ms / 1000.0)
            summary["pre_coef_frames"] = [f["cmd"] for f in pre_frames]
            summary["pre_coef_nacks"] = count_nacks(pre_frames)

        # Coefficient streams. AVR doesn't ACK coef packets, but it WILL
        # send a frame with body {"Comm":"NACK"} when a specific packet
        # is malformed or the stream falls behind. We drain a small
        # window after every packet to detect transient NACKs and
        # re-send the offending packet up to MAX_PACKET_RETRIES times
        # before giving up. NACKs seen here are application-layer
        # backpressure (firmware busy / NVRAM staging), not TCP loss —
        # retry is the standard remedy for this class of protocol.
        between_channel_pause_ms = 100.0
        per_packet_drain_ms = 8.0  # small window to catch a NACK
        retry_settle_ms = 250.0     # extra wait before resending
        MAX_PACKET_RETRIES = 3
        summary.setdefault("coef_packet_retries", 0)
        summary.setdefault("coef_packets_unrecovered", 0)
        for pkt, end_of_channel in coef_packet_streams:
            attempt = 0
            while True:
                sock.sendall(pkt)
                if inter_packet_delay_ms > 0:
                    time.sleep(inter_packet_delay_ms / 1000.0)
                summary["coef_packets_sent"] += 1
                # Drain a tiny window inline so NACKs are caught at
                # source instead of bleeding into the next packets.
                inline_frames = drain(per_packet_drain_ms / 1000.0)
                inline_nacks = count_nacks(inline_frames)
                if inline_nacks == 0:
                    break  # packet accepted (or no response yet — fine)
                # NACK observed for this packet. Retry with a longer
                # settle so the AVR's busy state can clear.
                if attempt >= MAX_PACKET_RETRIES:
                    summary["coef_packets_unrecovered"] += 1
                    summary["coef_nack_count"] += inline_nacks
                    break
                attempt += 1
                summary["coef_packet_retries"] += 1
                time.sleep(retry_settle_ms / 1000.0)
            if end_of_channel:
                # Drain briefly so any in-flight NACK frames land before
                # we move on to the next channel.
                between_frames = drain(between_channel_pause_ms / 1000.0)
                ch_nacks = count_nacks(between_frames)
                summary["coef_nack_per_channel"].append(ch_nacks)
                summary["coef_nack_count"] += ch_nacks

        # Allow the AVR to finish processing the coefficient bank.
        if coef_wait_init_ms > 0:
            time.sleep(coef_wait_init_ms / 1000.0)
        if coef_wait_final_ms > 0:
            # Drain anything the AVR queued during processing — some
            # firmwares emit a status frame here.
            mid_frames = drain(coef_wait_final_ms / 1000.0)
            mid_nacks = count_nacks(mid_frames)
            summary["coef_nack_count"] += mid_nacks
            summary["mid_nack_count"] = mid_nacks
            summary["mid_wait_frames"] = [
                {
                    "cmd": f["cmd"],
                    "data": f["data"][:120].decode("ascii", errors="replace"),
                }
                for f in mid_frames
            ]

        sock.sendall(build_frame("FINZ_COEFS"))
        finz_frames = drain(20.0)
        summary["finz_coefs_ack"] = did_ack(finz_frames, "FINZ_COEFS")
        summary["coef_nack_count"] += count_nacks(finz_frames)
        summary["finz_frames"] = [f["cmd"] for f in finz_frames]

        # Final commit gate. Refuse to send Fin if (a) commit_fin=False
        # was explicitly requested, or (b) any NACKs were observed in
        # the coef stream and abort_fin_on_nack is True. Committing on
        # a partial bank is the documented X3800H ChSetup-wipe trigger.
        should_commit = commit_fin
        if should_commit and abort_fin_on_nack and summary["coef_nack_count"] > 0:
            should_commit = False
            summary["fin_skipped_reason"] = (
                f"{summary['coef_nack_count']} NACK(s) observed during coef "
                f"stream — Fin commit aborted to prevent ChSetup wipe"
            )
        elif not commit_fin:
            summary["fin_skipped_reason"] = "commit_fin=False (caller requested no commit)"

        if should_commit:
            summary["fin_commit_attempted"] = True
            time.sleep(0.02)
            sock.sendall(build_frame("SET_SETDAT", b'{"AudyFinFlg":"Fin"}'))
            fin_frames = drain(5.0)
            summary["fin_commit_ack"] = did_ack(fin_frames, "SET_SETDAT")
            summary["fin_frames"] = [f["cmd"] for f in fin_frames]

        time.sleep(0.02)
        sock.sendall(build_frame("EXIT_AUDMD"))
        exit_frames = drain(2.0)
        summary["exit_audmd_ack"] = did_ack(exit_frames, "EXIT_AUDMD")
        summary["exit_frames"] = [f["cmd"] for f in exit_frames]
    finally:
        try:
            sock.close()
        except OSError:
            pass

    return summary


async def push_avr_filters(
    host: str,
    *,
    ady: dict,
    channel_filters: Mapping[str, Sequence[float]],
    avr_status: Mapping[str, object] | None = None,
    distances_override_m: Mapping[str, float] | None = None,
    calibration_settings: Mapping[str, object] | None = None,
    target_curves: Sequence[str] | None = None,
    samplerates_hz: Sequence[int] = XT32_SAMPLE_RATES_HZ,
    init_coefs_required: bool | None = None,
    coef_wait_init_ms: float | None = None,
    coef_wait_final_ms: float | None = None,
    inter_packet_delay_ms: float = 25.0,
    port: int = DEFAULT_PORT,
    timeout: float = 30.0,
    commit_fin: bool = True,
    abort_fin_on_nack: bool = True,
    pre_coef_settle_ms: float = 500.0,
) -> dict:
    """Upload custom FIR coefficients to one or more AVR channels via the
    Audyssey TCP/1256 protocol.

    Args:
        host: AVR IP / hostname.
        ady: parsed .ady JSON for envelope state. Provides per-channel
            distance, level, crossover, speaker-type — and the channel
            list.
        channel_filters: ``{channel_id: avr_format_coefs}`` map — coefs
            must already be polyphase-decimated to 1024 (speaker) or
            704 (sub) floats. Use ``calibrate.audyssey_fir.convert_xt32``
            to do that. Channels not in this map are not modified, but
            the envelope still includes their distance / level state.
        avr_status: GET_AVRSTS+GET_AVRINF response. If None, queried
            transiently before the upload.
        distances_override_m: optional override for one or more channels'
            envelope Distance values. Use to push a sub past the
            variance cap.
        calibration_settings: optional override of the runtime AudyDynEq
            / AudyEqRef / etc. defaults. Field types must match
            DEFAULT_CALIBRATION_SETTINGS.
        target_curves: which target-curve banks to write. Default writes
            both Flat (``"00"``) and Reference (``"01"``) so the user
            can toggle at runtime via AudyEqSet.
        samplerates_hz: which sample rates to ship per channel. Default
            is XT32's three (32k/44.1k/48k).
        init_coefs_required: if None (default) auto-detect from
            avr_status.DType (sent only when DType startsWith "fixed").
            Override to True/False to force.
        coef_wait_init_ms: pause between channels' coef streams.
        coef_wait_final_ms: pause after all coef streams before
            FINZ_COEFS. Set higher (~15 s) for slower receivers — the
            X3800H's CoefWaitTime.Final is 15000.
        inter_packet_delay_ms: pause between individual SET_COEFDT
            packets. Helps less-buffered receivers keep up.
        commit_fin: when True (default), send the AudyFinFlg=Fin commit
            after FINZ_COEFS to persist coefficients to NVRAM. Set False
            for non-destructive verification — the AVR's persisted state
            stays unchanged after EXIT_AUDMD. Use this for multi-channel
            wire-format verification before risking a real commit.
        abort_fin_on_nack: when True (default), refuse the Fin commit if
            ANY NACK frames were observed during the SET_COEFDT stream.
            Committing on a partially-corrupted coefficient bank is the
            documented X3800H ChSetup-wipe trigger.

    Returns:
        Summary dict with per-stage ACK status + per-channel NACK
        counts (``coef_nack_per_channel``) + total stream NACK count
        (``coef_nack_count``). ``ok`` is True only when every required
        ACK landed AND the Fin commit was attempted AND ACKed.
        ``fin_skipped_reason`` is populated when the Fin commit was
        gated by either ``commit_fin=False`` or NACK-on-stream.
    """
    if not channel_filters:
        raise ValueError("channel_filters is empty — nothing to upload")

    # Resolve AVR status if not provided.
    if avr_status is None:
        loop = asyncio.get_running_loop()
        avr_status = await loop.run_in_executor(None, query_avr_status, host)

    # Decide INIT_COEFS path. X3800H reports DType="Float" → no INIT_COEFS.
    if init_coefs_required is None:
        d_type = str(avr_status.get("DType", "")).lower()
        init_coefs_required = d_type.startswith("fixed")

    # Use the AVR's reported coefficient-processing wait times if the
    # caller didn't set explicit overrides. The X3800H reports
    # CoefWaitTime.Final = 15000 ms — without that pause FINZ_COEFS
    # times out without ACK because the DSP is still consuming the
    # coefficient stream.
    coef_wait_times = avr_status.get("CoefWaitTime") or {}
    if coef_wait_init_ms is None:
        coef_wait_init_ms = float(coef_wait_times.get("Init", 0)) if coef_wait_times else 20.0
    if coef_wait_final_ms is None:
        coef_wait_final_ms = float(coef_wait_times.get("Final", 15000)) if coef_wait_times else 15000.0

    # Build SET_SETDAT envelope.
    ordered = build_set_dat_envelope(
        ady,
        dict(avr_status),
        distances_override_m=distances_override_m,
        calibration_settings=calibration_settings,
    )
    setdat_chunks = chunk_setdat_payload(ordered)

    # Build SET_DISFIL bodies — one per (channel, EqType). The official
    # MultEQ Editor and the deleted scripts/audyssey_push_full_filters.py
    # (commit ea8fd76) send these AFTER the SET_SETDAT envelope and
    # BEFORE SET_COEFDT. Without SET_DISFIL the X3800H buffers our
    # coefficient streams but never engages them at runtime — the
    # symptom is silent MultEQ playback after a successful Fin commit.
    # Source: ratbuddyssey DisFil.cs:43-100, .ady fields
    # dispLargeData (FilData) and dispSmallData (DispData).
    setdisfil_bodies: list[dict] = []
    for ch in ady.get("detectedChannels", []):
        cid = ch.get("commandId")
        if not cid:
            continue
        # Only push DISFIL for channels we're actually uploading coefs
        # for. Other channels keep their existing DISFIL state.
        if cid not in channel_filters:
            continue
        large = ch.get("dispLargeData") or []
        small = ch.get("dispSmallData") or []
        for eq in ("Audy", "Flat"):
            setdisfil_bodies.append({
                "EqType": eq,
                "ChData": cid,
                "FilData": list(large),
                "DispData": list(small),
            })

    # Build coefficient packet streams in channel order.
    if target_curves is None:
        from calibrate.drivers.denon.audyssey_coef_transfer import (
            TARGET_CURVE_FLAT,
            TARGET_CURVE_REFERENCE,
        )
        target_curves = (TARGET_CURVE_FLAT, TARGET_CURVE_REFERENCE)

    # Coefficient packet order matters: A1Evo Acoustica uses outer-tc,
    # middle-channel, inner-sr. Mismatched order triggers per-packet
    # NACKs. See oca_transfer.py:1533-1543. Yield (packet, end_of_channel)
    # tuples so the sender can pause between channels.
    from calibrate.drivers.denon.audyssey_coef_transfer import (
        build_coef_packets,
    )
    coef_streams: list[tuple[bytes, bool]] = []
    for tc in target_curves:
        channel_list = list(channel_filters.items())
        for ch_idx, (cid, coefs) in enumerate(channel_list):
            sr_list = list(samplerates_hz)
            for sr_idx, sr in enumerate(sr_list):
                pkts = build_coef_packets(
                    coefs,
                    channel_id=cid,
                    target_curve=tc,
                    samplerate_hz=sr,
                )
                last_sr = sr_idx == len(sr_list) - 1
                last_ch = ch_idx == len(channel_list) - 1
                for pi, pkt in enumerate(pkts):
                    is_last_in_channel = (
                        pi == len(pkts) - 1 and last_sr
                    )
                    coef_streams.append((pkt, is_last_in_channel and not last_ch))

    loop = asyncio.get_running_loop()
    summary = await loop.run_in_executor(
        None,
        _push_full_sync,
        host,
        port,
        setdat_chunks,
        coef_streams,
        init_coefs_required,
        coef_wait_init_ms,
        coef_wait_final_ms,
        inter_packet_delay_ms,
        timeout,
        commit_fin,
        abort_fin_on_nack,
        pre_coef_settle_ms,
        setdisfil_bodies,
    )

    # ok = every protocol-required step succeeded AND the Fin commit
    # actually persisted (so callers can distinguish a successful push
    # from a non-destructive verification or a NACK-aborted commit).
    base_acks_ok = (
        summary.get("enter_audy_ack")
        and all(summary.get("setdat_acks", []))
        and (summary.get("init_coefs_ack") is None or summary["init_coefs_ack"])
        and summary.get("finz_coefs_ack")
        and summary.get("exit_audmd_ack")
    )
    summary["ok"] = bool(
        base_acks_ok
        and summary.get("fin_commit_attempted")
        and summary.get("fin_commit_ack")
    )
    # Separately surface "verification mode succeeded" — every ACK we
    # asked for landed, just no Fin commit was attempted.
    summary["verified"] = bool(base_acks_ok and not summary.get("fin_commit_attempted"))
    summary["channel_count"] = len(channel_filters)
    return summary


def channels_in_ady(ady: dict) -> list[str]:
    """Pull the list of channel commandIds present in an .ady file."""
    return [c["commandId"] for c in ady.get("detectedChannels", []) if c.get("commandId")]


def is_sub_channel_id(cid: str) -> bool:
    """Convenience re-export so callers don't import audyssey_fir for one symbol."""
    return is_sub_channel(cid)


__all__ = [
    "DEFAULT_CALIBRATION_SETTINGS",
    "SETDAT_PARAM_ORDER",
    "SET_SETDAT_CHUNK_THRESHOLD_BYTES",
    "build_set_dat_envelope",
    "channels_in_ady",
    "chunk_setdat_payload",
    "envelope_packet_size",
    "is_sub_channel_id",
    "push_avr_filters",
    "query_avr_status",
]
