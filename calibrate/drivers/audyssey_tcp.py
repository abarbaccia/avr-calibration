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

# Empirically measured on Denon X3800H (2026-04-25). The AVR firmware caps
# the actual applied per-channel delay at ~65 ms regardless of the pushed
# distance value — pushing 30.72 m, 50 m, and 100 m all produce identical
# ~65 ms of mains delay. The configured value persists, the *applied* delay
# does not exceed this. May differ on other Denon/Marantz models.
MAX_APPLIED_DELAY_MS: float = 65.0

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
) -> None:
    """Push speaker distances to a Denon/Marantz AVR via Audyssey TCP.

    With ``commit=False`` the change is volatile (lost on power cycle).
    With ``commit=True`` the AVR persists to NVRAM via ``AudyFinFlg=Fin``.

    Caller is responsible for any user confirmation per the
    "signal-path-writes need human approval" rule. This function does
    not prompt — it just executes what it's told.
    """
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
