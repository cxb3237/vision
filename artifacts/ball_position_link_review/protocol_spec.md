# Steel Ball Position Link Protocol

## Wire format

| Offset | Type | Name | Value |
| ---: | --- | --- | --- |
| 0 | uint8 | SOF1 | `0xA5` |
| 1 | uint8 | SOF2 | `0x5A` |
| 2 | int8 | x_mm | `-125..125` |
| 3 | uint8 | CRC | CRC-8/ATM over offsets 0..2 |

Packet length is always 4 bytes. There is no version, type, length, sequence, target class, Y coordinate,
confidence or detection-state field.

CRC parameters: polynomial `0x07`, initial value `0x00`, no input/output reflection, xorout `0x00`.

## Required vectors

- `-125 mm`: `A5 5A 83 86`
- `0 mm`: `A5 5A 00 06`
- `+125 mm`: `A5 5A 7D 72`

## Publication semantics

Only a newly produced, valid, calibrated position is published while position output is enabled. No target
means no packet. The sender keeps at most one pending position and never retransmits a consumed position.
The `AA 55` general VMC control, heartbeat and ACK protocol remains separate and unchanged.

## Pixel mapping

`x_mm = -125 + (center_x - x_minus_125_px) * 250 / (x_plus_125_px - x_minus_125_px)`

The value is rounded to the nearest integer and clamped to `-125..125`. Reversed endpoints are valid.
When `calibrated` is false, the mapping produces no publishable position.
