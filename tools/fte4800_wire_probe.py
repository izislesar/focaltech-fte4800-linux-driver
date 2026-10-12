#!/usr/bin/env python3
"""FTE4800 / FT9368 SPI wire-census probe (issue #1, Tecno MegaBook S14).

This tool is a read-only diagnostic. It never changes the production driver
and never writes firmware. It complements tools/fte4800_selftest.py by
exercising the wire at a level below the misc-device ABI, using the kernel
driver's IOCTL_RAW_XFER (0x80A0):

  * A/B reset-state test      - identity read with the reset pad held LOW
                                versus released, to prove the reset pad
                                physically controls the sensor.
  * Framing sweep             - write-then-read (two CS windows) vs two-phase
                                full-duplex (one CS window) vs raw RX-only.
  * Mode / speed sweep        - SPI modes 0-3, speeds 100 kHz - 8 MHz.
  * ROM-state ID probe        - read 0x0090, expect 0x56A2 while in ROM boot.
  * Vendor-wake variants      - FF 00 00 00 write before/after reset (the
                                SPI0_Wakeup pattern reverse engineered from
                                ftWbioUmdfDriverV2.dll), and CMD_Set(0x55).
  * Boot-timing sweep         - repeated identity reads from 0 ms to 1 s after
                                reset release.
  * Bulk-response profile     - 5120-byte reads of 0x9180 / 0x9080 with a
                                run-length census (FF / 00 / mid-level bytes).
  * IRQ census                - enable the sensor IRQ and count edges during
                                reset toggles.

Interpretation used by docs/issue-1-tecno-s14mm-wire-census.md:
  A healthy FT9368 answers identity reads at 0x9180 with
  '00 00 ff 00 .. .. 29 93 68 11 aa 40 50 ..' (chip id 0x9368). A sensor
  that is held in reset answers 0xFF. A sensor that never parses commands
  still presents a fixed, address-independent line-level artifact.
"""
from __future__ import annotations

import ctypes
import os
import time
import argparse

DEV = "/dev/focal_moh_spi"

IOCTL_RESET = 0x8086
IOCTL_POWER_OFF = 0x8087
IOCTL_POWER_ON = 0x8088
IOCTL_IRQ_ENABLE = 0x8089
IOCTL_RAW_XFER = 0x80A0

RAW_XFER_MAX = 16
RAW_XFER_BUF = 16384
RAW_NOTX = 0x01
RAW_NORX = 0x02

INFO_REG = 0x9180
IMAGE_REG = 0x9080
ROM_ID_REG = 0x0090
INFO_BYTES = 32
CHIP_OFFSET = 0x13
CHIP_ID = 0x9368

WAKE_PATTERN = bytes((0xFF, 0x00, 0x00, 0x00))  # SPI0_Wakeup (4-byte, no RX)
CMD_SET_55 = bytes((0x70, 0x55, 0xAA))          # CMD_Set(0x55), sync/prepare
SFR_WRITE = 0x70, 0x07, 0xF8                    # CMD_Set(0x07), SFR write

IRQ_PROC = "/proc/interrupts"

libc = ctypes.CDLL(None, use_errno=True)
libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
libc.ioctl.restype = ctypes.c_int


class RawStep(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_uint32),
        ("delay_us", ctypes.c_uint16),
        ("cs_change", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
    ]


class RawRequest(ctypes.Structure):
    _fields_ = [
        ("n", ctypes.c_uint32),
        ("mode", ctypes.c_uint32),
        ("speed_hz", ctypes.c_uint32),
        ("pre_delay_us", ctypes.c_uint32),
        ("x", RawStep * RAW_XFER_MAX),
        ("tx", ctypes.c_uint8 * RAW_XFER_BUF),
        ("rx", ctypes.c_uint8 * RAW_XFER_BUF),
    ]


class Wire:
    """IOCTL_RAW_XFER wrapper. steps: list of (tx|None, rxlen, delay_us, cs)."""

    def __init__(self, path: str = DEV):
        self.fd = os.open(path, os.O_RDWR)

    def close(self) -> None:
        os.close(self.fd)

    def raw(self, steps, mode: int = 0xFFFFFFFF, speed_hz: int = 0) -> bytes:
        req = RawRequest()
        req.n = len(steps)
        req.mode = mode
        req.speed_hz = speed_hz
        tx_off = rx_off = 0
        for i, (tx, rxlen, delay_us, cs) in enumerate(steps):
            txlen = 0 if tx is None else len(tx)
            req.x[i].len = txlen + rxlen
            req.x[i].delay_us = delay_us
            req.x[i].cs_change = 1 if cs else 0
            flags = 0
            if txlen == 0 and rxlen:
                flags |= RAW_NOTX
            if txlen and rxlen == 0:
                flags |= RAW_NORX
            req.x[i].flags = flags
            if tx:
                for j, byte in enumerate(tx):
                    req.tx[tx_off + j] = byte
            tx_off += txlen
            rx_off += rxlen
        buf = (ctypes.c_ubyte * ctypes.sizeof(req)).from_buffer(req)
        if libc.ioctl(self.fd, IOCTL_RAW_XFER, buf) < 0:
            raise OSError(ctypes.get_errno(), "IOCTL_RAW_XFER")
        return bytes(req.rx[:rx_off])

    # framings -------------------------------------------------------------
    def read_wtr(self, addr: int, n: int, mode=0xFFFFFFFF, speed=0) -> bytes:
        """Verified healthy form: TX header (CS1) then RX payload (CS2)."""
        hdr = bytes((addr >> 8, addr & 0xFF, (n >> 8) & 0xFF, n & 0xFF, 0, 0, 0))
        return self.raw([(hdr, 0, 1, 1), (None, n, 0, 0)], mode=mode, speed_hz=speed)

    def read_fdx(self, addr: int, n: int, mode=0xFFFFFFFF, speed=0) -> bytes:
        """One CS window: 7-byte header + n zeros, RX taken back-to-back."""
        hdr = bytes((addr >> 8, addr & 0xFF, (n >> 8) & 0xFF, n & 0xFF, 0, 0, 0))
        return self.raw([(hdr + b"\x00" * n, n, 0, 0)], mode=mode, speed_hz=speed)

    def read_rxonly(self, n: int, mode=0xFFFFFFFF, speed=0) -> bytes:
        """No TX at all: MISO line census while the clock runs."""
        return self.raw([(None, n, 0, 0)], mode=mode, speed_hz=speed)

    def write(self, tx: bytes, delay_us: int = 0) -> bytes:
        return self.raw([(tx, 0, delay_us, 1)])

    # ioctls ---------------------------------------------------------------
    def reset(self, boot_wait: float = 0.35) -> None:
        libc.ioctl(self.fd, IOCTL_RESET, None)
        if boot_wait:
            time.sleep(boot_wait)

    def power_off(self) -> None:   # reset pad asserted (LOW for active-high pad)
        libc.ioctl(self.fd, IOCTL_POWER_OFF, None)

    def power_on(self) -> None:    # reset pad released
        libc.ioctl(self.fd, IOCTL_POWER_ON, None)

    def irq(self, on: bool) -> None:
        libc.ioctl(self.fd, IOCTL_IRQ_ENABLE, ctypes.c_void_p(1 if on else 0))


def hx(b: bytes, limit: int = 0) -> str:
    s = b.hex(" ")
    if limit and len(b) > limit:
        s = " ".join(f"{x:02x}" for x in b[:limit])
    return s


def chip_id(r: bytes) -> int:
    return (r[CHIP_OFFSET] << 8) | r[CHIP_OFFSET + 1] if len(r) >= CHIP_OFFSET + 2 else -1


def describe(label: str, r: bytes, n_print: int = 48) -> None:
    runs: list[tuple[str, int]] = []
    for byte in r:
        cls = "FF" if byte == 0xFF else ("00" if byte == 0 else "XX")
        if runs and runs[-1][0] == cls:
            runs[-1] = (cls, runs[-1][1] + 1)
        else:
            runs.append((cls, 1))
    summary = []
    ff = zeros = mid = 0
    for byte in r:
        ff += byte == 0xFF
        zeros += byte == 0
        mid += byte not in (0, 0xFF)
    print(f"{label}: chip@0x13=0x{chip_id(r):04X} | FFs={ff} zeros={zeros} mid={mid} "
          f"| runs={runs if len(runs) <= 6 else runs[:3] + [('...', 0)] + runs[-2:]}")


# ---------------------------------------------------------------------------
# sections

def section_reset_ab(w: Wire) -> None:
    print("\n== [1] reset A/B test: MISO line level with reset pad low vs high ==")
    w.power_off()
    time.sleep(0.05)
    dxl = 32
    r_low = w.read_fdx(INFO_REG, dxl)
    w.power_on()
    time.sleep(0.35)
    r_high = w.read_fdx(INFO_REG, dxl)
    describe("  reset LOW  ", r_low)
    describe("  reset HIGH ", r_high)


def section_framing(w: Wire) -> None:
    print("\n== [2] framing sweep @1MHz mode0 ==")
    w.reset()
    r = w.read_wtr(INFO_REG, INFO_BYTES)
    describe("  write-then-read", r)
    r = w.read_fdx(INFO_REG, INFO_BYTES)
    describe("  full-duplex-xfer", r)
    r = w.read_wtr(INFO_REG, 64)
    describe("  wtr 64B        ", r)


def section_mode_speed(w: Wire) -> None:
    print("\n== [3] SPI mode sweep ==")
    for m in range(4):
        w.reset(0.05)
        r = w.read_wtr(INFO_REG, INFO_BYTES, mode=m)
        describe(f"  mode={m}", r)
    print("== [4] SPI speed sweep (mode0) ==")
    for s in (100_000, 250_000, 500_000, 1_000_000, 2_000_000, 4_000_000, 8_000_000):
        w.reset(0.05)
        r = w.read_wtr(INFO_REG, INFO_BYTES, speed=s)
        describe(f"  {s//1000:5d} kHz", r)


def section_rom(w: Wire) -> None:
    print("\n== [5] ROM-state ID (0x0090, healthy ROM value 0x56A2) ==")
    w.reset()
    r = w.read_wtr(ROM_ID_REG, 2)
    describe("  wtr 2B", r)
    r = w.read_fdx(ROM_ID_REG, 32)
    describe("  fdx 32B", r)


def section_wake(w: Wire) -> None:
    print("\n== [6] vendor wake / sync variants ==")
    w.reset(); w.write(WAKE_PATTERN); time.sleep(0.005)
    describe("  reset,wake,+5ms", w.read_wtr(INFO_REG, INFO_BYTES))

    w.power_off(); time.sleep(0.02); w.write(WAKE_PATTERN)
    w.power_on(); time.sleep(0.005)
    describe("  low->wake->rel +5ms", w.read_wtr(INFO_REG, INFO_BYTES))
    describe("  low->wake->rel +350ms", w.read_wtr(INFO_REG, INFO_BYTES))

    w.reset(); w.write(CMD_SET_55); time.sleep(0.010)
    describe("  reset->CMD55 +10ms", w.read_wtr(INFO_REG, INFO_BYTES))

    w.reset(); w.write(CMD_SET_55)
    for wait_ms in (20, 50, 100, 200, 300, 500):
        r = w.read_wtr(INFO_REG, INFO_BYTES)
        describe(f"  reset->CMD55 +{wait_ms}ms", r)


def section_boot_timing(w: Wire) -> None:
    print("\n== [7] boot-timing sweep after reset (fdx, 250 ms spacing) ==")
    w.reset(boot_wait=0)
    t0 = time.time()
    for _ in range(6):
        describe(f"  t={time.time()-t0:5.2f}s", w.read_fdx(INFO_REG, INFO_BYTES))
        time.sleep(0.25)


def section_bulk(w: Wire) -> None:
    print("\n== [8] bulk-response profile (5120 B, one CS window) ==")
    w.reset()
    for addr, label in ((INFO_REG, "0x9180 identity"), (IMAGE_REG, "0x9080 image")):
        describe(f"  bulk {label}", w.read_fdx(addr, 5120), 0)
    # the real capture sequence: SFR write then waited integration, bulk read
    sfr = bytes((SFR_WRITE[0], SFR_WRITE[1], SFR_WRITE[2],
                 0x00, 0x3B, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00))
    w.write(sfr)
    time.sleep(0.06)
    describe("  bulk after sfr 0x3B=1", w.read_fdx(IMAGE_REG, 5120), 0)


def section_irq(w: Wire) -> None:
    print("\n== [9] IRQ census during reset toggles ==")

    def irq_count() -> int:
        with open(IRQ_PROC) as fh:
            for line in fh:
                if "focal-irq" in line:
                    return int(line.split()[1])
        return -1

    w.irq(True)
    try:
        for _ in range(4):
            before = irq_count()
            w.power_off()
            time.sleep(0.05)
            w.power_on()
            time.sleep(1.0)
            print(f"  IRQ count {before} -> {irq_count()}")
    finally:
        w.irq(False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default=DEV)
    ap.add_argument("--skip", default="",
                    help="comma list of sections to skip: reset_ab,framing,"
                         "mode_speed,rom,wake,boot_timing,bulk,irq")
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()} - {""}

    w = Wire(args.device)
    try:
        for section in (section_reset_ab, section_framing, section_mode_speed,
                        section_rom, section_wake, section_boot_timing,
                        section_bulk, section_irq):
            if section.__name__.removeprefix("section_") in skip:
                continue
            section(w)
        print("\nWIRE PROBE DONE")
    finally:
        w.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
