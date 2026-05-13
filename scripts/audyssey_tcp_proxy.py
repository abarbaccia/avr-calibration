"""Transparent TCP proxy on port 1256 — captures Audyssey MultEQ Editor traffic.

Runs on the Pi. The phone's MultEQ Editor app connects here (manual IP entry)
instead of directly to the AVR; this proxy forwards bytes to the real AVR
and logs every framed message in both directions.

USAGE
    sudo python3 audyssey_tcp_proxy.py 192.168.1.209 \\
        --listen 0.0.0.0:1256 \\
        --log /tmp/multeq-capture.log \\
        --raw /tmp/multeq-capture.bin

`--raw` writes the concatenated byte stream of each direction to two files
(.tx and .rx suffixes) for offline re-parsing. `--log` is the human-readable
frame-by-frame trace.

Requires sudo because port 1256 is privileged on most distros (<1024 is
strictly privileged but 1256 is fine — but if you redirect 80/443 you'd
need sudo). Listed for completeness.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import struct
import sys
import threading
import time

DEFAULT_PORT = 1256
HEADER_LEN = 9
CMD_LEN = 10


def parse_frames(stream: bytearray) -> list[dict]:
    """Drain complete frames from the front of `stream`. Mutates."""
    frames: list[dict] = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if total_len < 19 or total_len > 0xFFFF:
            del stream[0]
            continue
        if len(stream) < total_len:
            break
        cur_pkt = stream[3]
        tot_pkt = stream[4]
        cmd = bytes(stream[5:15]).decode("ascii", errors="replace")
        data_len = struct.unpack(">H", bytes(stream[16:18]))[0]
        data = bytes(stream[18:18 + data_len])
        ck_actual = stream[total_len - 1]
        ck_expected = sum(stream[:total_len - 1]) & 0xFF
        frames.append({
            "cmd": cmd.strip(),
            "data": data,
            "dir_byte": chr(stream[0]),
            "cur": cur_pkt,
            "tot": tot_pkt,
            "ck_ok": ck_actual == ck_expected,
            "raw_len": total_len,
        })
        del stream[:total_len]
    return frames


def fmt_body_preview(data: bytes, max_len: int = 200) -> str:
    """Pretty-print body — JSON if possible, otherwise hex+lengths."""
    if not data:
        return "(empty)"
    # Try ASCII JSON
    try:
        text = data.decode("ascii")
        try:
            obj = json.loads(text)
            compact = json.dumps(obj, separators=(",", ":"))
            if len(compact) > max_len:
                compact = compact[:max_len] + f"...[+{len(compact) - max_len}]"
            return f"json: {compact}"
        except json.JSONDecodeError:
            return f"ascii: {text[:max_len]!r}"
    except UnicodeDecodeError:
        pass
    # Binary: dump first 16 Int32 as both signed and as float32
    n_ints = min(16, len(data) // 4)
    int_view = struct.unpack(f">{n_ints}i", data[:n_ints * 4])
    float_view = struct.unpack(f">{n_ints}f", data[:n_ints * 4])
    head = f"binary {len(data)}B; "
    head += "first ints: [" + ", ".join(f"0x{v & 0xFFFFFFFF:08x}" for v in int_view[:6]) + "...]; "
    head += "as float32: [" + ", ".join(f"{v:.4g}" for v in float_view[:6]) + "...]"
    return head


class Logger:
    def __init__(self, path: str | None):
        self.path = path
        self.fh = open(path, "w") if path else None
        self.lock = threading.Lock()
        self.t0 = time.monotonic()

    def log(self, msg: str) -> None:
        ts = f"{time.monotonic() - self.t0:8.3f}"
        line = f"[{ts}] {msg}"
        with self.lock:
            print(line)
            if self.fh:
                self.fh.write(line + "\n")
                self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()


def pump(
    src: socket.socket,
    dst: socket.socket,
    direction: str,  # "phone→avr" or "avr→phone"
    raw_path: str | None,
    logger: Logger,
) -> None:
    rxbuf = bytearray()
    raw_fh = open(raw_path, "wb") if raw_path else None
    try:
        while True:
            try:
                chunk = src.recv(65536)
            except (ConnectionResetError, OSError) as e:
                logger.log(f"[{direction}] recv error: {e}")
                break
            if not chunk:
                logger.log(f"[{direction}] closed")
                break
            if raw_fh:
                raw_fh.write(chunk)
                raw_fh.flush()
            try:
                dst.sendall(chunk)
            except (ConnectionResetError, OSError) as e:
                logger.log(f"[{direction}] forward error: {e}")
                break
            rxbuf.extend(chunk)
            for f in parse_frames(rxbuf):
                tag = "tx-frame" if f["dir_byte"] == "T" else "rx-frame"
                ck = "" if f["ck_ok"] else " CHECKSUM-FAIL"
                preview = fmt_body_preview(f["data"])
                logger.log(
                    f"[{direction}] {tag} cmd={f['cmd']!r} cur={f['cur']} "
                    f"tot={f['tot']} dlen={len(f['data'])}{ck}\n"
                    f"             body: {preview}"
                )
    finally:
        if raw_fh:
            raw_fh.close()
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve_one(client: socket.socket, peer: tuple, args: argparse.Namespace, logger: Logger) -> None:
    logger.log(f"=== new connection from {peer[0]}:{peer[1]} → forwarding to {args.target}:{args.target_port}")
    try:
        upstream = socket.create_connection((args.target, args.target_port), timeout=10.0)
    except OSError as e:
        logger.log(f"upstream connect failed: {e}")
        client.close()
        return

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    raw_tx = f"{args.raw}.{ts}.phone-to-avr.bin" if args.raw else None
    raw_rx = f"{args.raw}.{ts}.avr-to-phone.bin" if args.raw else None

    t1 = threading.Thread(target=pump, args=(client, upstream, "phone→avr", raw_tx, logger), daemon=True)
    t2 = threading.Thread(target=pump, args=(upstream, client, "avr→phone", raw_rx, logger), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    try:
        client.close()
    except OSError:
        pass
    try:
        upstream.close()
    except OSError:
        pass
    logger.log("=== connection closed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="real AVR IP (e.g. 192.168.1.209)")
    ap.add_argument("--target-port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--listen", default="0.0.0.0:1256", help="host:port to listen on")
    ap.add_argument("--log", default="/tmp/multeq-capture.log")
    ap.add_argument("--raw", default="/tmp/multeq-capture",
                    help="prefix for raw byte logs (.<ts>.{phone-to-avr,avr-to-phone}.bin)")
    args = ap.parse_args()

    host, _, port = args.listen.rpartition(":")
    listen_host = host or "0.0.0.0"
    listen_port = int(port)

    logger = Logger(args.log)
    logger.log(f"listening on {listen_host}:{listen_port}, target={args.target}:{args.target_port}")
    logger.log(f"log={args.log} raw_prefix={args.raw}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host, listen_port))
    srv.listen(4)

    try:
        while True:
            client, peer = srv.accept()
            threading.Thread(target=serve_one, args=(client, peer, args, logger), daemon=True).start()
    except KeyboardInterrupt:
        logger.log("shutting down")
    finally:
        srv.close()
        logger.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
