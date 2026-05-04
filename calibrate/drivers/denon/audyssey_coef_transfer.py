"""SET_COEFDT packet builder for direct-uploading FIR coefficients to a
Denon/Marantz AVR via the Audyssey TCP protocol on port 1256.

Each AVR channel (speaker or sub) accepts one polyphase-decimated FIR
of fixed length:
    speaker: 1024 floats (4-band polyphase, decimated from 16,321 taps)
    sub:     704 floats  (4-band polyphase, decimated from 16,055 taps)

The FIR is shipped per (target_curve, sample_rate) tuple. XT32 stores
two target curves on the AVR (``00`` Flat, ``01`` Reference) and three
sample rates (``00`` 32 kHz, ``01`` 44.1 kHz, ``02`` 48 kHz). Every
channel × tc × sr combination needs its own packet stream.

Packet layout (variable-length, ``buildAvrPacket`` style):

    1 byte    marker 0x54
    2 bytes   total_length (BE)
    1 byte    seq_num  (this packet's index in the per-stream sequence)
    1 byte    last_seq_num (final index — same in every packet)
    10 bytes  "SET_COEFDT"
    1 byte    0x00 (separator)
    2 bytes   param_length (BE) — bytes after this header up to checksum
    [4 bytes  first packet only: tc(1) + sr(1) + channel_byte(1) + 0x00]
    N bytes   coefficient payload (4 bytes per BE float32)
    1 byte    checksum (sum of preceding bytes & 0xFF)

Float layout per packet:
    first packet: 127 floats
    mid packets:  128 floats each
    last packet:  remainder

Ported from srinivas486/audyssey-rew-tuner (MIT-licensed):
- `oca_transfer.py:build_packet_config` — packet count + float-per-packet math
- `oca_transfer.py:generate_coef_packets` — frame builder per stream
"""
from __future__ import annotations

import struct
from typing import Sequence

from calibrate.audyssey_fir import SAMPLE_RATE_CODES, get_channel_byte

MARKER = 0x54
SET_COEFDT_BYTES = b"SET_COEFDT"

# Target-curve identifiers that the AVR expects on the wire. Both are
# stored in flash; user toggles between them at runtime via AudyEqSet.
TARGET_CURVE_FLAT = "00"
TARGET_CURVE_REFERENCE = "01"

# Sample rates that XT32 expects in the upload. Each FIR is shipped
# three times — once per rate — and the AVR picks the right one for
# the active source at playback.
XT32_SAMPLE_RATES_HZ: tuple[int, ...] = (32000, 44100, 48000)


def packet_config_for(total_floats: int) -> dict:
    """Return ``{packetCount, firstPacketFloats, midPacketFloats,
    lastPacketFloats, lastSeqNum}`` for a stream of ``total_floats``
    floats.

    The AVR expects the first packet to carry 127 floats, mid packets
    128 each, and the last whatever remains.
    """
    first = 127
    mid = 128
    if total_floats <= 0:
        return {
            "packet_count": 0,
            "first_packet_floats": 0,
            "mid_packet_floats": mid,
            "last_packet_floats": 0,
            "last_seq_num": 0,
        }
    if total_floats <= first:
        return {
            "packet_count": 1,
            "first_packet_floats": total_floats,
            "mid_packet_floats": mid,
            "last_packet_floats": total_floats,
            "last_seq_num": 0,
        }
    remaining = total_floats - first
    additional = (remaining + mid - 1) // mid
    packet_count = 1 + additional
    last_remainder = remaining % mid
    last_packet_floats = last_remainder if last_remainder else mid
    return {
        "packet_count": packet_count,
        "first_packet_floats": first,
        "mid_packet_floats": mid,
        "last_packet_floats": last_packet_floats,
        "last_seq_num": packet_count - 1,
    }


def _build_coef_packet(
    *,
    seq_num: int,
    last_seq_num: int,
    tc: str | None,
    sr: str | None,
    channel_byte: int | None,
    coefficient_bytes: bytes,
) -> bytes:
    """Frame a single SET_COEFDT packet.

    For the first packet of a stream pass ``tc``, ``sr``, and
    ``channel_byte`` — they encode into a 4-byte stream header
    (target_curve(1) + sample_rate(1) + channel(1) + 0x00). For
    subsequent packets pass ``None`` for all three.
    """
    is_first = seq_num == 0
    if is_first:
        if tc is None or sr is None or channel_byte is None:
            raise ValueError("first packet requires tc, sr, channel_byte")
        # tc/sr are hex-string codes; channel_byte is an int.
        first_packet_info = bytes.fromhex(
            tc + sr + format(channel_byte & 0xFF, "02x") + "00"
        )
        param_data = first_packet_info + coefficient_bytes
    else:
        param_data = coefficient_bytes

    param_length = len(param_data)
    # Header: SET_COEFDT(10) + 0x00(1) + param_length(2 BE)
    command_header = SET_COEFDT_BYTES + b"\x00" + struct.pack(">H", param_length)
    # Full frame: marker(1) + length(2 BE) + seq(1) + last_seq(1) + header + data + checksum(1)
    total_length = 1 + 2 + 1 + 1 + len(command_header) + param_length + 1

    body = (
        bytes([MARKER])
        + struct.pack(">H", total_length)
        + bytes([seq_num & 0xFF])
        + bytes([last_seq_num & 0xFF])
        + command_header
        + param_data
    )
    checksum = sum(body) & 0xFF
    return body + bytes([checksum])


def build_coef_packets(
    coefficients: Sequence[float],
    *,
    channel_id: str,
    target_curve: str,
    samplerate_hz: int,
    mult_eq_type: str = "XT32",
) -> list[bytes]:
    """Pack a per-channel FIR coefficient vector into the AVR's
    SET_COEFDT packet stream.

    Args:
        coefficients: 1024 (speaker) or 704 (sub) AVR-format floats —
            i.e. already polyphase-decimated by ``audyssey_fir.convert_xt32``.
        channel_id: Audyssey channel commandId, e.g. "FL", "SW1", "TRL".
        target_curve: ``"00"`` Flat or ``"01"`` Reference.
        samplerate_hz: 32000, 44100, or 48000.
        mult_eq_type: defaults to XT32. Use "XT" / "MultEQ" for older
            receivers (different channel-byte mapping).

    Returns:
        Ordered list of packets ready to send back-to-back over the
        TCP socket.
    """
    if target_curve not in (TARGET_CURVE_FLAT, TARGET_CURVE_REFERENCE):
        raise ValueError(
            f"target_curve must be '00' or '01', got {target_curve!r}"
        )
    if samplerate_hz not in SAMPLE_RATE_CODES:
        raise ValueError(
            f"unsupported samplerate {samplerate_hz} — expected one of "
            f"{sorted(SAMPLE_RATE_CODES.keys())}"
        )
    sr_code = SAMPLE_RATE_CODES[samplerate_hz]
    channel_byte = get_channel_byte(channel_id, mult_eq_type)

    # Pre-pack each float as 4 big-endian bytes. The wire format is
    # big-endian — confirmed by pcap-decode of real AVR traffic
    # (scripts/audyssey_pcap_decode.py reads coefficients as ">f")
    # and by ratbuddyssey's FloatInt32 union (parser.cs:18-35), which
    # treats coefs as float32 bits read as signed Int32 BE. The prior
    # little-endian encoding was inherited from a flawed comment in the
    # audyssey-rew-tuner port and explained the persistent ~1-2% packet
    # NACK rate plus FINZ_COEFS-never-ACKs failure mode on multi-channel
    # uploads (a fraction of garbage-decoded floats blew past the AVR's
    # coefficient-validity check, aborting the commit).
    coef_words: list[bytes] = [
        struct.pack(">f", float(c)) for c in coefficients
    ]

    cfg = packet_config_for(len(coef_words))
    packets: list[bytes] = []
    floats_sent = 0
    for packet_index in range(cfg["packet_count"]):
        is_first = packet_index == 0
        is_last = packet_index == cfg["packet_count"] - 1
        if is_first:
            count = cfg["first_packet_floats"]
        elif is_last:
            count = cfg["last_packet_floats"]
        else:
            count = cfg["mid_packet_floats"]

        # Don't run past the end on the last packet.
        if floats_sent + count > len(coef_words):
            count = len(coef_words) - floats_sent

        payload = b"".join(coef_words[floats_sent: floats_sent + count])
        packets.append(
            _build_coef_packet(
                seq_num=packet_index,
                last_seq_num=cfg["last_seq_num"],
                tc=target_curve if is_first else None,
                sr=sr_code if is_first else None,
                channel_byte=channel_byte if is_first else None,
                coefficient_bytes=payload,
            )
        )
        floats_sent += count

    return packets


def all_streams_for_channel(
    coefficients: Sequence[float],
    *,
    channel_id: str,
    mult_eq_type: str = "XT32",
    target_curves: Sequence[str] = (TARGET_CURVE_FLAT, TARGET_CURVE_REFERENCE),
    samplerates_hz: Sequence[int] = XT32_SAMPLE_RATES_HZ,
) -> list[bytes]:
    """Generate every (target_curve, sample_rate) packet stream for a
    single channel, flattened into one ordered byte-stream-friendly list.

    Total packets = len(target_curves) × len(samplerates_hz) ×
    packets_per_stream. For a speaker FIR (1024 floats, 9 packets per
    stream) at default settings: 2 × 3 × 9 = 54 packets per channel.
    For a sub (704 floats, 6 packets per stream): 2 × 3 × 6 = 36.
    """
    out: list[bytes] = []
    for tc in target_curves:
        for sr in samplerates_hz:
            out.extend(
                build_coef_packets(
                    coefficients,
                    channel_id=channel_id,
                    target_curve=tc,
                    samplerate_hz=sr,
                    mult_eq_type=mult_eq_type,
                )
            )
    return out
