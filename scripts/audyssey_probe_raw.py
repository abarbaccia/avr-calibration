"""Raw byte dump from Denon Audyssey TCP — no framing assumptions."""
import socket, struct, sys, time

HOST, PORT = "192.168.1.209", 1256
HEADER_LEN = 9
CMD_LEN = 10


def frame(cmd: str, data: bytes = b"") -> bytes:
    cb = cmd.encode("ascii")
    assert len(cb) == CMD_LEN
    total = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total)
    buf += b"\x00\x00"
    buf += cb
    buf.append(0)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def main():
    s = socket.create_connection((HOST, PORT), timeout=8)
    s.settimeout(2.0)
    rx = bytearray()

    def drain(seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                s.settimeout(max(0.05, end - time.monotonic()))
                c = s.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not c:
                break
            rx.extend(c)

    print(f"connected to {HOST}:{PORT}")
    drain(1.0)  # any unsolicited greeting?
    if rx:
        print(f"unsolicited rx ({len(rx)} bytes):")
        print(rx[:512].hex())

    for cmd in ("ENTER_AUDY", "GET_AVRINF"):
        f = frame(cmd)
        print(f"\n>>> tx {cmd}: {f.hex()}")
        s.sendall(f)
        before = len(rx)
        drain(3.0)
        new = bytes(rx[before:])
        print(f"<<< rx {len(new)} bytes")
        print(new[:1024].hex())
        if len(new) > 1024:
            print(f"... +{len(new)-1024} more bytes")

    s.sendall(frame("EXIT_AUDMD"))
    drain(0.5)
    s.close()
    print(f"\ntotal rx bytes: {len(rx)}")


if __name__ == "__main__":
    main()
