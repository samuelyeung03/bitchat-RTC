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
| Role | ADB serial | Device | Notes |
|------|-----------|--------|-------|
| Pi 5 #1 (sender) | `798f51f064cce0d1` | RPi5/AOSP | Razer Kiyo X on `/dev/video0` |
| Pi 5 #2 (receiver) | `f501a6221ec14252` | RPi5/AOSP | no camera |
| Phone sender | `T1AIOC656909KGK` | ASUS ROG Phone 9 (AI2501C) | YUV file or camera |
| Phone receiver | `dc1c0ad` | Redmi Note 7 (lavender) | Android 9, MTU=256 |

Both Pis run AOSP 16 (eng.samuel). Phones run stock Android.
YUV test file on ASUS: `/data/local/tmp/complex_320x240.yuv` (push from `test/media/`).

**CRITICAL — debug prefs:** `bitchat_debug_settings.xml` persists across installs (not cleared on uninstall on rooted devices). After any `stop_client`/`stop_server` ADB command, these prefs are written and survive restart, silently breaking the mesh. Always reset after testing:
```bash
cat > /tmp/debug_prefs.xml << 'XML'
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <int name="max_connections_client" value="8" />
    <boolean name="gatt_client_enabled" value="true" />
    <boolean name="gatt_server_enabled" value="true" />
    <boolean name="verbose_logging" value="true" />
    <int name="max_connections_server" value="8" />
    <int name="max_connections_overall" value="8" />
</map>
XML
for S in T1AIOC656909KGK dc1c0ad; do
  adb -s $S push /tmp/debug_prefs.xml /data/local/tmp/debug_prefs.xml
  adb -s $S shell "run-as com.bitchat.droid cp /data/local/tmp/debug_prefs.xml /data/data/com.bitchat.droid/shared_prefs/bitchat_debug_settings.xml"
done
```

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

# ADB command interface (AdbActivity — zero-UI, am start)
adb -s <serial> shell am start -n com.bitchat.droid/com.bitchat.android.AdbActivity \
    --es cmd <COMMAND> [extras]

# BLE-specific logcat
adb -s <serial> shell logcat -s BluetoothMeshService BluetoothConnectionManager \
    BluetoothGattClientManager BluetoothGattServerManager PeerManager RTCConnectionManager
```

## AdbActivity commands
| cmd | extra args | what it does |
|-----|-----------|--------------|
| `peer_id` | — | logs local peer ID → `ADB_CMD:I  PEER_ID <hex>` |
| `peers` | — | logs all verified peers → `ADB_CMD:I  PEER id=<hex> nick=<nick>` |
| `start_video` | `--es peer_id <hex>` `--ei cl <-99/-1/0-9>` `--ei fps <n>` [`--es src <path>` `--ei w <w>` `--ei h <h>`] | start DACE video; cl=-99=OFF, cl=-1=DACE auto, cl=0-9=fixed |
| `stop_video` | — | stop video |
| `set_complexity` | `--ei cl <-1..9>` | change CL on running encoder |
| `stop_client` | — | stop BLE client (scanner) |
| `start_client` | — | start BLE client |
| `stop_server` | — | stop BLE server (advertiser) |
| `start_server` | — | start BLE server |
| `stop_scan` | — | stop BLE scanning only |
| `start_scan` | — | start BLE scanning only |
| `connect_to` | `--es addr <BLE_MAC>` | pin to single peer, stop scan flood (use to avoid 133 errors) |
| `unpin` | — | resume normal multi-peer scanning |

---

## Build & install
```bash
cd bitchat-RTC
./gradlew assembleDebug
adb -s T1AIOC656909KGK install -r app/build/outputs/apk/debug/app-debug.apk
adb -s dc1c0ad install -r app/build/outputs/apk/debug/app-debug.apk
# Pi5s (when connected):
adb -s 798f51f064cce0d1 install -r app/build/outputs/apk/debug/app-debug.apk
adb -s f501a6221ec14252 install -r app/build/outputs/apk/debug/app-debug.apk
```

## Test results (2026-04-16, phones, BLE mesh, 320×240 @3fps, 100kbps)

### DACE ON vs OFF — ASUS ROG9 → Redmi Note 7

| Mode      | SEND | RECV | PSNR avg | Enc avg |
|-----------|------|------|----------|---------|
| DACE ON (auto, cl=-1) | 90 | 1 | 30.0 dB | 42 ms |
| DACE OFF (cl=-99)     | 90 | 0 | 29.8 dB | 5 ms |

**Notes:**
- RECV very low (fragment loss) — each 3KB frame = ~28 fragments at 180B/fragment.
  At 3fps, ~84 fragments/s exceeds sustained BLE throughput on Redmi Note 7.
- DACE ON: 42ms encode (adapting to scene complexity on Snapdragon 8 Gen3)
- DACE OFF: 5ms encode (plain x264 CL0 settings, 8× faster)
- PSNR difference ~0.25 dB — negligible; DACE effect on quality is bitrate-limited
- **Next step**: lower bitrate to 20-40kbps (→ ~600-1200B/frame = 4-8 fragments) for reliable delivery

### Known phone-specific issues (vs Pi5s)
- **MTU=256** on phones (vs MTU=517 on Pi5s) → `MAX_FRAGMENT_SIZE` must be ≤180B (not 500B)
- **VBV buffering**: x264 buffers output until VBV buffer fills. Set `i_vbv_buffer_size=0` (disabled)
  to get immediate frame output. On Pi5s the VBV buffer filled fast enough to be invisible.
- **Debug prefs corruption**: `stop_client`/`stop_server` ADB commands persist prefs
  (`max_connections=1`, `gatt_client_enabled=false`) across restarts. Always reset (see above).
- **BLE status 133 flood**: random BLE addressing causes many failed connect attempts.
  Use `connect_to --es addr <MAC>` to pin to one peer after scanning briefly.

## Test results (2026-04-08, Pi5, BLE mesh, 320×240 @5fps, 100kbps)

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
- Be concise — no summaries, no preamble
- Commit after each meaningful change
- DACE param: `param.dace=0/1` is on/off switch; `dace_complexity_level=-1` = truly auto
- cl=-99 via ADB = DACE OFF (sentinel value → sets param.dace=0 in JNI)
- Use `encode_bench.py` for fast encoder-only sweeps; use `ble_psnr_test.py` for full BLE end-to-end
- Prefer `--src /data/local/tmp/complex_320x240.yuv` over camera for reproducible results
- After ADB stop_client/stop_server, ALWAYS reset debug prefs (see Hardware setup section)
- ble_psnr_test.py: use `--sender`/`--receiver` for phone serials; script auto-detects peer ID
