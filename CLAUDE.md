# BitChat-RTC — Claude Working Notes

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
- DACE OFF = `param.dace=0` (do NOT change analysis params, just flip the flag)
- DACE auto ON = `param.dace=1`, `dace_complexity_level=-1`
- Fixed CL = `param.dace=1`, `dace_complexity_level=0..9`

### PSNR test infrastructure (native, TCP-based)
Located in `test/`:
- `dace_hw_bench` — C binary already pushed to both Pi5s under `/data/local/tmp/`
- `dace_compare.py` — runs DACE on/off + CL sweep over **TCP** between Pi1 (cam) and Pi2
- `dace_psnr_bench` — PSNR bench binary also on devices
- Previous results: `/data/local/tmp/sender_*.csv`, `receiver_*.csv`

> The TCP bench (`dace_compare.py`) is a **separate baseline**, not through BLE mesh.
> Goal: route video through actual BLE mesh and get equivalent PSNR numbers.

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

## Test results (2026-04-08, BLE mesh, 320×240 @15fps, 100kbps)

| Mode   | PSNR avg | Enc avg | NAL avg | Recv/254 |
|--------|----------|---------|---------|----------|
| auto   | 53.8 dB  | 22 ms   | 806 B   | ~9       |
| CL0    | 52.0 dB  |  4 ms   | 825 B   | ~8       |
| CL1    | 50.2 dB  |  5 ms   | 830 B   | ~16      |
| CL2    | 52.6 dB  |  6 ms   | 830 B   | ~2       |
| CL3    | 52.3 dB  |  7 ms   | 842 B   | 0        |
| CL4    | 52.1 dB  | 13 ms   | 852 B   | ~7       |
| CL5    | 53.3 dB  | 11 ms   | 842 B   | ~4       |

**Key observations:**
- PSNR 50-54 dB is **encoder-side** (x264 reconstructed vs input). BLE transport does not affect PSNR.
- Encode time scales with CL as expected (CL0=4ms, CL5=11ms, auto=22ms).
- **Packet loss ~97%** at 15fps — BLE mesh throughput (~1-2 KB/s video) can't sustain 15fps × 850B/frame.
- Next: reduce to 5fps or use `set_complexity` mid-stream to get cleaner latency measurements.
- Latency when received: 50-500ms (sparse, BLE congestion; δ≈-19ms between Pi1 and Pi2 clocks).
- Script: `python3 test/ble_psnr_test.py --duration 20 --cls -1 0 1 2 3 4 5`

## User preferences
- Do NOT use the TCP bench as a substitute for BLE-mesh testing
- Commit after each meaningful change
- Be concise — no summaries, no preamble
- DACE param: `param.dace = 0/1` is the on/off switch; `dace_complexity_level = -1` = truly auto
