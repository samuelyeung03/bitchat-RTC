# BitChat-RTC — Claude Working Notes

> **Claude: read this file at the start of every session and after every meaningful change. Keep it up to date — it is your primary source of truth for this project.**


## Project overview
Android peer-to-peer chat + voice/video app using **Bluetooth LE mesh** as the sole transport.
No internet required. Devices discover each other via BLE scan/advertise, exchange messages,
voice, and video over fragmented GATT packets.

**Package:** `com.bitchat.droid`  
**Code namespace:** `com.bitchat.android`  
**Min SDK:** 26 (Android 8)  **Target:** 34  
**NDK:** 27.2.12479018 (arm64-v8a)

---

## Hardware setup
| Role | ADB serial | Notes |
|------|-----------|-------|
| Pi 5 #1 (sender) | `798f51f064cce0d1` | Razer Kiyo X on `/dev/video0` |
| Pi 5 #2 (receiver) | `f501a6221ec14252` | no camera |

Both devices run Android on Raspberry Pi 5. Always connected via USB-ADB.

---

## Key components

### BLE mesh transport
- `BluetoothMeshService` — top-level orchestrator; holds `rtcConnectionManager`
- `BluetoothConnectionManager` — starts server + client, manages power mode
- `BluetoothGattClientManager` — BLE scanning + GATT connects (duty-cycle aware)
- `BluetoothGattServerManager` — GATT server, advertising
- `FragmentManager` — fragments large packets to ≤469 B BLE payloads
- BLE service UUID: `f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c`

### Video codec (DACE)
- `DACEEncoder` / `DACEDecoder` — Kotlin wrappers
- `DACEWrapper` — JNI bridge to native `dacewrapper.so`
- `dace_jni.cpp` — x264 DACE encoder; `param.dace=1` enables DACE, `param.dace=0` disables
- `RTCConnectionManager` — orchestrates audio + video encode/send/recv/decode
- Default: 320×240 @ 15 fps, 100 kbps, **`param.dace=1` + `dace_complexity_level=-1` (auto)**
- DACE CL range: **-1 (auto) and 0–9** (fixed)
- DACE OFF = `param.dace=0` (do NOT change analysis params, just flip the flag)
- DACE auto ON = `param.dace=1`, `dace_complexity_level=-1`
- Fixed CL = `param.dace=1`, `dace_complexity_level=0..9`

### Test infrastructure
Located in `test/`:
- `encode_bench.py` — encode-only benchmark; runs on Pi1 only, no BLE needed. Reads SEND logcat for PSNR + encode time per CL. Supports `--src` YUV file.
- `ble_psnr_test.py` — full BLE mesh test; measures PSNR, encode time, and end-to-end latency. Supports `--src` YUV file.
- `media/` — YUV test files: `complex_320x240.yuv`, `complex_640x360.yuv`, `complex_1920x1080.yuv`

---

## ADB commands
```bash
# Grant all permissions (run once after install)
for PERM in BLUETOOTH_ADVERTISE BLUETOOTH_CONNECT BLUETOOTH_SCAN \
            ACCESS_FINE_LOCATION ACCESS_COARSE_LOCATION RECORD_AUDIO \
            CAMERA POST_NOTIFICATIONS; do
  adb -s <serial> shell pm grant com.bitchat.droid android.permission.$PERM
done

# Launch app
adb -s <serial> shell am start -n com.bitchat.droid/com.bitchat.android.MainActivity

# Send ADB command to the app (via AdbCommandReceiver — see below)
adb -s <serial> shell am broadcast \
    -a com.bitchat.droid.CMD \
    -n com.bitchat.droid/.AdbCommandReceiver \
    --es cmd <COMMAND> [extras]

# BLE-specific logcat
adb -s <serial> shell logcat -s BluetoothMeshService BluetoothConnectionManager \
    BluetoothGattClientManager BluetoothGattServerManager PeerManager RTCConnectionManager
```

## AdbCommandReceiver commands
| cmd | extra args | what it does |
|-----|-----------|--------------|
| `peer_id` | — | logs local peer ID to logcat tag `ADB_CMD` |
| `start_video` | `--es peer_id <hex>` | calls `rtcConnectionManager.startVideo(...)` |
| `stop_video` | — | calls `rtcConnectionManager.stopVideo()` |
| `start_bench_recv` | — | (future) enable bench receiver mode |
| `start_bench_send` | `--es peer_id <hex>` `--ez dace_on true/false` `--ei frames 150` | (future) start bench sender |

---

## Build & install
```bash
cd bitchat-RTC
./gradlew assembleDebug
adb -s 798f51f064cce0d1 install -r app/build/outputs/apk/debug/app-debug.apk
adb -s f501a6221ec14252 install -r app/build/outputs/apk/debug/app-debug.apk
```

## Test results (2026-04-08, BLE mesh, 320×240 @5fps, 100kbps)

### Encoder-side PSNR + timing (definitive, all CLs)

| Mode   | PSNR avg | PSNR min | Enc avg | NAL avg |
|--------|----------|----------|---------|---------|
| auto   | 42.7 dB  | 36.0 dB  | 46 ms   | 2567 B  |
| CL0    | 43.4 dB  | 42.4 dB  |  6 ms   | 2496 B  |
| CL1    | 43.0 dB  | 40.3 dB  |  5 ms   | 2416 B  |
| CL2    | 43.2 dB  | 41.3 dB  |  9 ms   | 2439 B  |
| CL3    | 43.6 dB  | 42.7 dB  | 12 ms   | 2519 B  |
| CL4    | 43.9 dB  | 43.0 dB  | 18 ms   | 2512 B  |
| CL5    | 43.9 dB  | 43.0 dB  | 18 ms   | 2517 B  |

**Key findings:**
- **Encode time** scales clearly with CL: CL0=6ms, CL1=5ms, CL2=9ms, CL3=12ms, CL4/5=18ms, auto=46ms.
- **PSNR is roughly constant (~43 dB)** across all fixed CLs — DACE CL affects CPU cost, not quality (bitrate-limited).
- **DACE auto (CL=-1) has 46ms encode time** — 8× slower than CL0 — adapting to scene complexity. No PSNR gain vs fixed CLs.
- **NAL size stable ~2400-2570 B** — encoder fills the 100kbps budget regardless of CL.
- PSNR is **encoder-side** (x264 reconstructed vs input). BLE transport does not affect this metric.

### BLE transport observations
- **Packet delivery: ~3-37% at 5fps** — 5fps × 2500B/frame = 12.5 KB/s exceeds sustained BLE mesh budget.
- Each frame = ~6 BLE fragments (2500B / 469B). At 10ms pacing = 60ms per frame.
- `CONNECTION_PRIORITY_HIGH` (7.5ms interval) set, MTU=517. Theoretical max ~35 KB/s but practical is lower.
- `WRITE_TYPE_NO_RESPONSE` + `notifyCharacteristicChanged(confirm=false)` applied for video.
- Remaining bottleneck: Android GATT stack serialises writes; need ≥1 connection interval between writes.
- **Next steps**: reduce bitrate to 40kbps (→ ~1000B/frame = 3 fragments = 30ms/frame), or drop to 2fps.

### ADB test commands
```bash
# Encode-only (Pi1 only, no BLE needed)
python3 test/encode_bench.py --src /data/local/tmp/complex_320x240.yuv             # full sweep: auto + CL0-9
python3 test/encode_bench.py --src /data/local/tmp/complex_320x240.yuv --cls -1 0  # quick DACE on/off

# BLE mesh end-to-end
python3 test/ble_psnr_test.py --src /data/local/tmp/complex_320x240.yuv --duration 20  # full sweep
python3 test/ble_psnr_test.py --duration 30 --cls -1 0  # quick, live camera
```

### Known hardware issue
- Pi1 (798f) loses clock on power-off → always run `adb -s 798f51f064cce0d1 root && adb shell date -s @$(date +%s)` after Pi1 reboots.
- Staggered startup required: start Pi1 first, wait 8s, then start Pi2 so Pi2 scans and finds Pi1.

## User preferences
- Use `encode_bench.py` for fast encoder-only sweeps; use `ble_psnr_test.py` for full end-to-end BLE tests
- Commit after each meaningful change
- Be concise — no summaries, no preamble
- DACE param: `param.dace = 0/1` is the on/off switch; `dace_complexity_level = -1` = truly auto
