# Issue #1 wire census — Tecno MegaBook S14 (FTE4800, chip-id 0xFFFF)

Complete, reproducible evidence package collected on the reporter's hardware:

    Machine:      TECNO MegaBook S14 (S14MM SKU), Core Ultra 9 185H, Arch Linux
    Kernel:       7.2.9-arch1-1 (v7.2.8-1 at report time)
    ACPI device:  \_SB.PC02.SPI1.FPNT (_ADR 0x001E0003 = PCI 00:1e.3),
                  enumerated by Linux as pxa2xx-spi.2 / spi-FTE4800:00
    Driver:       focal_spi DKMS v1.0.3, source identical to git main
                  (md5 4303a10024ac718f0f6746d4aa388efd), SRCVERSION 0B82973762AB252FA2D5FC6
    Selftest:     reset PASS; identity always 0xFFFF; RESULT: FAIL

All tool output quoted below is reproducible with `tools/fte4800_wire_probe.py`.

## 1. What is already verified working

* ACPI/DSDT configuration is complete and matches the template used by the
  working Infinix ZERO BOOK 13 path:
  * `FPTT == 0x07` -> HID `FTE4800` (confirmed live, modalias `acpi:FTE4800`)
  * `FPTM == 0` -> `_CRS = SPIM(SPIX=0x07, mode 0, 0x000F4240 Hz) + BBUF + GBUF`
    i.e. exactly 1 MHz, mode 0, one GpioIo reset pad, one GpioInt; no extra
    GpioIo (UBUF) resource present.
  * Reset pad `\ PullUp`/IoRestrictionOutputOnly, GpioInt Edge/ActiveHigh.
* The kernel bound it as claimed:
  * `focal-fte4800 : ready: mode=0 speed=1000000Hz bits=8 irq=66 reset=active-high`
  * debugfs gpio: `gpio-208 (GPP_B_4) spi-FTE4800:00 out hi` — the reset pad
    **is** claimed and actively driven HIGH.
  * IRQ 66 is the correct GpioInt route (`INUM(GFPI) & 0x7F == 66`), requested
    as rising edge — consistent with `GpioInt (Edge, ActiveHigh, ...)`.
* The reset pad demonstrably **physically controls the sensor** (see §2, A/B test).

## 2. Wire census results (IOCTL_RAW_XFER)

### [1] Reset A/B test — the sensor is attached and alive

| State | Identity read (32 B, full-duplex) |
|---|---|
| Reset pad asserted LOW | `ff ff ff ff ... ff` (all FF) |
| Reset pad released HIGH | `00×20 xx ff×11` (MISO clamped LOW, then released) |

With reset LOW the sensor never drives MISO (pure pull-up level). After
release it clamps MISO LOW at the next CS assert. This proves the ACPI reset
pad reaches the sensor; the driver's reset handling is correct.

### [3]/[4] Mode and speed sweep — the "reply" is a line-level artifact, not data

Transition point of MISO LOW→HIGH vs SPI clock (mode 0, write-then-read):

| Clock | zeros before release | elapsed time while LOW |
|---|---|---|
| 100 kHz | 8 B | ~80 µs |
| 250 kHz | 11 B | ~44 µs |
| 500 kHz | 16 B | ~32 µs |
| 1 MHz | 26 B | ~26 µs |
| 2 MHz | 32 B (whole read) | ≥16 µs |
| 4 MHz | 32 B | ≥8 µs |
| 8 MHz | 32 B | ≥4 µs |

* SPI mode (0/1/2/3) does not change the pattern (a real framing mismatch
  would).
* The release point is roughly constant **in time**, not in bytes, and the
  release edge itself is a slow RC ramp (partial-byte masks 01/03/07/0F/1F/7F
  observed across reads): classic "driven low, then left high-Z with a weak
  pull-up".
* Reads of different addresses (identity 0x9180, image 0x9080, ROM 0x0090,
  2 B / 32 B / 5120 B) produce byte-for-byte the same census → the sensor
  never parses the 7-byte command header.
* Repeated reads after the first post-reset read return pure FF: the clamp is
  a one-shot behavior tied to reset release, not a per-transfer response.
* Deep reads (5120 B) contain exactly the same ~20 zeros + FF tail — no data
  anywhere in a 5120-byte window.

### [5] ROM-state ID probe

`SPI0_Read(0x90)` never returns the documented ROM bootloader ID `0x56A2`
(only the line-level artifact). The sensor is therefore not in the
normal ROM state either.

### [6] Vendor recovery sequences — no effect

The known vendor sequences were applied before identity reads, none produced
application-mode behavior:

* `SPI0_Wakeup` pattern `FF 00 00 00` after reset (+5 ms) — unchanged.
* `FF 00 00 00` written *while the reset pad is LOW*, then release
  (+5 ms / +350 ms) — unchanged.
* `CMD_Set(0x55)` = `70 55 AA` boot sync, waits 10…500 ms — unchanged.

So the `FT9368.py`-only wake sequence cannot explain the failure either;
the opt-in `--wake-probe` in commit 0742fce still reports FAIL on this
machine.

### [9] IRQ census

The GpioInt (rising edge, IRQ 66) fires **never** — including across several
power-off/power-on cycles and an 8-second observation window
(`/proc/interrupts` count stays 0; baseline was also 0 for the whole syslog
since module load in a pristine boot). Even if SPI data were missing, a
booting FT9368 would be expected to signal attention through IRQ at least
once. Consistent with a chip that never leaves reset/pre-boot state.

## 3. Interpretation

The device binding, ACPI resources, reset polarity, SPI mode/speed and IRQ
route are all correct and identical in shape to the working reference board.
On the wire, the FTE4800:

1. is physically attached (reset pad toggling changes MISO level),
2. never produces a single command-shaped response,
3. does not parse the protocol framing used by the healthy FT9368,
4. never raises its interrupt.

The remaining explanations, in order of likelihood:

* **H1 — board/SKU-level power or strap.** The S14MM fingerprint module may
  lack or have disabled its 3.3 V rail / a strapping pin, so the MCU never
  executes flash or ROM code. (Consistent with: correct line-level artifact
  behavior + zero command parsing + zero IRQ.)
* **H2 — flex/connector fault on this unit.** Same observable signature.
* **H3 — this SKU ships a different FocalTech sensor family** that needs the
  vendor PRAMBOOT firmware download sequence (registers 0x09/0x10/0x61,
  poll 0x6A, 0xAB/0xBF download loop) which the Linux stack never requires
  (it is not ported because Infinix flash-boots). The ROM handshake failed
  earlier than the identity read (no 0x56A2), so this cannot yet be started
  from the current transport.

Distinguishing H3 from H1/H2 requires either the vendor's Tecno S14 Windows
driver behaviour on the same unit (does Windows fingerprint enroll work?) or
a logic analyser on the flex (are MISO bytes actually driven low by the
sensor pad or by a level shifter / pull-down).

## 4. How to reproduce

    sudo python3 tools/fte4800_wire_probe.py 2>&1 | tee wire-census.log

Attach: `wire-census.log`, `dmesg-current.log`,
selftest `wake-probe.log`, and (if available) a decompiled DSDT excerpt for
`\_SB.PC02.SPI1.FPNT`.

## 5. Suggested next steps

1. Confirm Windows behaviour on the same SKU (did the fingerprint work in
   Windows on this machine before Arch was installed?). This is the highest
   -value single data point for H1 vs H3.
2. Loan/pass through controller debug: capture the SPI pads with a scope
   around the ~30 µs MISO-low pulse and confirm whether the pulse comes from
   the sensor pad (H1/H2) or from a stuck driver (H3-start).
3. Optionally: port the PRAMBOOT handshake from the Windows driver RE
   (boot-loader sequence, `SPI0_Read(0x90) == 0x56A2` gate) into
   `tools/ft9368.py` as an opt-in `--pramboot` probe; safe because the
   current transport never reaches that code path on flash-booted devices.
