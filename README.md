# ChargerDash for SkyRC iMAX B6AC v2

Live local dashboard for one or more **SkyRC iMAX B6AC v2** chargers, read over USB.
Click a charger's name to rename it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="screenshots/charging-dark.png">
  <img src="screenshots/charging.png" alt="ChargerDash showing a 4S LiIon balance charge: pack voltage, current, per-cell voltages and internal resistance, and live charts">
</picture>

<details>
<summary>More screenshots</summary>

**All chargers:** one row per charger, with each cell's voltage and internal resistance.

![All chargers overview](screenshots/all-chargers.png)

**Phone:** click the 📱 address in the header and scan the QR code with a phone on the same WiFi.

<img src="screenshots/phone.png" width="300" alt="ChargerDash on a phone">

</details>

## Download and run

1. Download **ChargerDash.exe** from the [Releases](../../releases) page and double-click it. Nothing to install.
2. The first time, Windows may show **"Windows protected your PC"** (SmartScreen). Click
   **More info → Run anyway**. That's normal for small apps that aren't code-signed.
3. Close SkyRC ChargeMaster, so the two programs don't fight over the charger.
4. Plug the charger into the PC with its USB cable. ChargerDash finds it within a few seconds.
5. Using more than one charger? Give each an ID by setting its **Waste time** to a different
   value (anything but the default 10 min, e.g. 11, 12). The in-app **Help** explains how.

**Use at your own risk.** ChargerDash is an unofficial hobby tool, not made or supported by SkyRC.
Only the iMAX B6AC v2 has been tested, with the packs and programs listed under [Tested](#tested). LiPo packs can
catch fire: never leave a charge unattended, and charge on a fireproof surface or in a LiPo bag.

## Features

**Start…** runs a balance charge or storage for LiPo, LiIon or LiHV. The cell count is detected
from the balance lead. It refuses to start if the balance lead is missing or has a gap, or if any
cell is below 3.00 V or above the chosen type's full charge. The charger doesn't report the main
lead while idle; it checks the cell count against the main lead itself as the program starts, and
the dashboard warns if the two leads disagree while it runs. The **Cells** picker (1S–6S) starts at the detected count;
if you set it to something else, Start stays off until the two agree. **Stop** ends the running program.

Each panel shows pack health warnings: cell imbalance (30 / 100 / 200 mV), a cell drifting
away from the rest during a program, low (< 3.30 V) or over-discharged (< 3.00 V) cells, cells
above full charge, main and balance leads disagreeing (while running), a cell with much higher internal resistance
than the rest, and a hot charger. Internal resistance per cell and for the pack is measured from the
voltage step when charge current starts (charger's 0 A check, then the first reading at 90% of the
set current), so it needs the app running when the program starts. Thresholds are in
`HEALTH` at the top of the warnings code in `static/index.html`.

Current has 0.5 / 1.0 / 1.5 / 2.0 A quick picks plus a custom box. Each charger's last battery,
program and current choices are saved, so every browser opens with them.
It shows pack voltage, current, mAh, elapsed time, charger temperature and each balance
cell's voltage, with live charts. Every running program is also logged to a CSV file.

Names (`chargers.json`), saved settings (`settings.json`) and charge logs (`logs\`) live in
**Documents\ChargerDash**. Set `CHARGERDASH_DATA` to use a different folder.

## Tested

On two SkyRC iMAX B6AC v2 chargers (machine id 100008, firmware 1.13), each run started with
ChargerDash's **Start** button and watched live:

| Battery | Cells | Program | Result |
|---|---|---|---|
| LiPo | 6S | Balance charge | Charged to full and finished |
| LiPo | 6S | Storage | Finished at storage voltage |
| LiPo | 4S | Balance charge | Charged to full and finished, both chargers |
| LiPo | 3S | Balance charge | Charging normally |
| LiHV | 2S | Balance charge | Charging normally to the 4.35 V LiHV limit |
| LiIon | 4S | Balance charge | Charging normally to the 4.10 V LiIon limit |

The charger reads each program back with the type, cell count and mode it was sent, and while
running its main-lead voltage matched the balance-lead total to within 0.03 V.

Not tested yet: **1S** and **5S** packs, LiIon on chargers with machine id 100130 (which use
type 2 for LiIon), and any charger other than the B6AC v2. Use these with extra care, and please report how
they go.

## Run

**App:** double-click `dist\ChargerDash.exe`. The dashboard opens in its own window, and closing
the window quits. Nothing else needs installing; it uses the Edge WebView2 runtime built into
Windows. The header shows the address to open on your phone (same WiFi); click it for a QR code
to scan. The first time, Windows
Firewall may ask about network access; allow it on private networks for phone access.

**From source:**

```bash
pip install -r requirements.txt
python app.py
```

`python server.py` runs just the server with no window, for opening in a browser at http://localhost:8765.

**Build the exe:** run `build.bat`, which writes `dist\ChargerDash.exe`.

Close SkyRC ChargeMaster first, so the two programs don't fight over the charger.

## Protocol notes (reverse-engineered, firmware 1.13)

USB HID, VID `0x0000` PID `0x0001`. Send the 64-byte report `0F 03 55 00 55 FF FF 00…`.
The reply is `0F 22 55 00 …`, with big-endian u16 values:

| Offset | Running (byte 4 = 1) or finished (byte 4 = 3) |
|---|---|
| 4 | mode (1 = running, 3 = finished, 2 = idle) |
| 5–6 | mAh |
| 7–8 | elapsed seconds |
| 9–10 | pack mV |
| 11–12 | current mA |
| 13–14 | internal temp °C (likely) |
| 17–28 | cells 1–6, mV |
| 35 | checksum = sum(bytes 2..34) & 0xFF |

Mode 4 in the status reply means the program stopped with an error, and the error code sits
where mAh normally is (e.g. 13 = balance connection error, 15 = cell number incorrect).

Other read-only queries use the same shape, `0F 03 <cmd> 00 <cmd> FF FF`:

- `0x5F` program: payload `state | batt type | cells | ? | max charge A×10 | max discharge A×10 | ? | mode | …`.
  Types are LiPo, LiIon, LiFe, LiHV, NiMH, NiCd, Pb (machine id 100130 swaps LiIon and LiFe).
  Li modes are Charge, Discharge, Storage, Fast, Balance.
- `0x57` machine id: ASCII at bytes 5–10, `100008` on this charger.

Commands (taken from SkyRC ChargeMaster's own code), each acknowledged with `F0 FF FF`:

- Stop: `0F 03 FE 00 FE FF FF`
- Start: `0F 16 05 00 | type cells mode | charge mA (u16) | discharge mA (u16) | min mV | max mV | 8 × 00 | checksum FF FF`,
  where type 0 = LiPo, 1 = LiIon (2 on machine id 100130), 3 = LiHV, and mode is 0 charge,
  1 discharge, 2 storage, 3 fast, 4 balance. Min/max mV per cell: LiPo 3200/4200,
  LiIon 3100/4100, LiHV 3000/4350.
  The checksum is the sum of every byte from `05` through the last `00`, & 0xFF.
  For example, 6S storage at 1 A is `0F 16 05 00 00 06 02 03 E8 03 E8 0C 80 10 68 00 00 00 00 00 00 00 00 E7 FF FF`.

When idle (mode 2), the status reply carries no live readings, only leftover bytes from earlier
replies. Idle cell voltages come from the `0x5A` system reply instead (`0F 25 5A 00 …`):
cells from byte 20. Bytes 18–19 read 15–16 V whatever pack is connected (4S or 6S alike), so
they're most likely the charger's DC input, not the main lead; the main lead isn't reported while
idle. The finished (mode 3) status reply also keeps the last program's pack voltage, so the
dashboard reads cells from `0x5A` whenever no program is running.

The chargers have no USB serial number. The dashboard uses each charger's **Waste time** setting
as its ID (10 min, the factory default, means no ID); chargers without an ID are told apart by
USB port. Confirmed live in the Start command: types 0 (LiPo), 1 (LiIon) and 3 (LiHV), modes
2 (storage) and 4 (balance), with 2, 3, 4 and 6 cells.

## Licence

MIT, see [LICENSE](LICENSE). Not affiliated with SkyRC.

The exe bundles Python and these open-source libraries, each under its own licence:
[hidapi](https://github.com/trezor/cython-hidapi) (BSD/GPL-3.0, used under BSD),
[pywebview](https://github.com/r0x0r/pywebview) (BSD-3-Clause) and
[segno](https://github.com/heuer/segno) (BSD-3-Clause, for the phone QR code).
