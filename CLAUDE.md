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
| Phone sender | `T1AIOC656909KGK` | ASUS ROG Phone 9 (AI2501C, Snapdragon 8 Gen3) | YUV file or camera |
| Phone receiver | `dc1c0ad` | Redmi Note 7 (lavender, Android 9) | MTU=517 after fix |

Both Pis run AOSP 16 (eng.samuel). Phones run stock Android.
YUV test file on ASUS: `/data/local/tmp/complex_320x240.yuv` (push from `test/media/`).
Reference x264 config: `~/libtest/test_x264.cpp` — uses `superfast` preset + zero-latency flags.

**CRITICAL — debug prefs:** `bitchat_debug_settings.xml` persists across installs. After any
`stop_client`/`stop_server` ADB command, prefs are silently corrupted. Always reset:
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
- `BluetoothMeshService` — top-level orchestrator; holds `rtcConnectionManager`; `instance` singleton
- `BluetoothConnectionManager` — starts server + client, manages power mode
- `BluetoothGattClientManager` — BLE scanning + GATT connects; per-device `Semaphore(1)` write flow control
- `BluetoothGattServerManager` — GATT server, advertising; per-device `Semaphore(1)` notification flow control
- `BluetoothPacketBroadcaster` — fragments + sends; `clientWriteAwaiter` + `serverNotifyAwaiter` hooks
- `FragmentManager` — fragments large packets; `MAX_FRAGMENT_SIZE=180` (phones, MTU≤256 era, now MTU=517)
- BLE service UUID: `f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c`

### Video codec (DACE)
- `DACEEncoder` / `DACEDecoder` — Kotlin wrappers
- `DACEWrapper` — JNI bridge to native `dacewrapper.so`
- `dace_jni.cpp` — x264 DACE encoder; matches `~/libtest/test_x264.cpp` reference config
- `RTCConnectionManager` — orchestrates audio + video encode/send/recv/decode
- Default: 320×240 @ 3 fps, 40 kbps (phones)

**DACE cl ADB mapping:**
| `--ei cl N` | `param.dace` | `dace_complexity_level` | Meaning |
|---|---|---|---|
| `cl=0` | 0 | — | **DACE OFF** (plain x264, superfast preset) |
| `cl=-1` | 1 | -1 | **DACE ON auto** (self-regulates complexity) |
| `cl=1-9` | 1 | N | **DACE ON fixed** (benchmark specific CLs) |

**DO NOT** use `-99` — removed. `cl=0` is DACE OFF.
psy-RD disabled (`b_psy=0`) for objective PSNR/SSIM.
SSIM available: `daceEnc.getLastSsimY()` — logged as `ssim=X.XXXX` in SEND log.

**x264 encoder config (dace_jni.cpp):**
- `x264_param_default_preset(&param, "superfast", "ssim")` → then `apply_profile("baseline")`
- ABR: `i_bitrate=br/1000`, `i_vbv_max_bitrate=br/1000`, `i_vbv_buffer_size=0` (no VBV delay)
- `rc.i_aq_mode=1` (adaptive quantisation)
- Zero-latency: `i_lookahead=0`, `i_sync_lookahead=0`, `i_bframe=0`, `b_sliced_threads=1`, `b_vfr_input=0`, `rc.b_mb_tree=0`
- `b_psnr=1`, `b_ssim=1`, `b_psy=0`, `f_psy_rd=0`, `f_psy_trellis=0`
- **IDR spike is unavoidable** with ABR+no-VBV: seq 0 ≈22KB, then ABR corrects. Use steady-state PSNR (skip first 2 frames) for fair comparison.

### BLE throughput (current state)
- **Observed: ~70 kbps** (phones, WNR + 2M PHY + DLE)
- **WNR** (`WRITE_TYPE_NO_RESPONSE`): always used for video — `onCharacteristicWrite` fires at stack-enqueue level for WNR, semaphore still works
- **2M PHY**: requested both client-side (in `onMtuChanged`) AND server-side (in `onConnectionStateChange`)
- **DLE**: MTU=517 auto-negotiated; `MAX_FRAGMENT_SIZE=180` is now conservative — could increase to ~460 for better efficiency on phones (currently 180 was set for old 256B MTU era)
- **Notification semaphore**: `onNotificationSent` releases permit for server-notify path
- **Duplicate connection dedup**: `isPeerAlreadyConnected(peerID)` in `BluetoothMeshService` drops redundant bidirectional connections that split bandwidth
- **Semaphore(2) HURTS**: Android 9 ATT discards 2nd in-flight WNR → keep `Semaphore(1)`
- **RECV rate**: ~13-20% at 40kbps (1911B/frame ÷ 180B/frag = ~11 fragments → P(deliver) ≈ 14%)
- **Next improvement**: increase `MAX_FRAGMENT_SIZE` from 180 to ~460 (MTU=517 → 517-55=462B payload) → 1911/460 = 5 frags → ~40% delivery

### Test infrastructure
Located in `test/`:
- `ble_psnr_test.py` — full BLE mesh PSNR test; use `--sender T1AIOC656909KGK --receiver dc1c0ad --bitrate 40000 --fps 3`
- `encode_bench.py` — encode-only (no BLE), Pi1 only
- `media/` — YUV test files: `complex_320x240.yuv`, `complex_640x360.yuv`, `complex_1920x1080.yuv`

**Latest results CSVs** (phone test 2026-04-16):
- `test/results_phones_dace_on_60s_v3.csv` — DACE ON 60s run
- `test/results_phones_dace_off_60s_v3.csv` — DACE OFF 60s run
- `test/results_phones_dace_comparison_v3.txt` — summary

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

# Video PSNR test (phones)
adb -s T1AIOC656909KGK shell am start -n com.bitchat.droid/com.bitchat.android.AdbActivity \
  --es cmd start_video --es peer_id <RECV_PEER_HEX> \
  --ei cl -1 --ei fps 3 --ei bitrate 40000 \
  --es src /data/local/tmp/complex_320x240.yuv --ei w 320 --ei h 240

# Push YUV test file to phone
adb -s T1AIOC656909KGK push test/media/complex_320x240.yuv /data/local/tmp/complex_320x240.yuv

# BLE logcat tags of interest
adb -s <serial> shell logcat -s latency:I BLE_THROUGHPUT:I ADB_CMD:I dace_jni:I
```

## AdbActivity commands
| cmd | extra args | what it does |
|-----|-----------|--------------|
| `peer_id` | — | logs `ADB_CMD:I  PEER_ID <hex>` |
| `peers` | — | logs `ADB_CMD:I  PEER id=<hex> nick=<nick>` |
| `start_video` | `--es peer_id <hex>` `--ei cl <0/-1/1-9>` `--ei fps <n>` `--ei bitrate <bps>` [`--es src <path>` `--ei w <w>` `--ei h <h>`] | cl=0=OFF, cl=-1=auto, cl=1-9=fixed |
| `stop_video` | — | stop video |
| `set_complexity` | `--ei cl <-1..9>` | change CL on running encoder |
| `stop_client` | — | stop BLE client (**resets debug prefs — reset after!**) |
| `start_client` | — | start BLE client |
| `stop_server` | — | stop BLE server (**resets debug prefs — reset after!**) |
| `start_server` | — | start BLE server |
| `stop_scan` | — | stop BLE scanning only |
| `start_scan` | — | start BLE scanning |
| `connect_to` | `--es addr <BLE_MAC>` | pin to one peer; stops 133-flood from random MAC rotation |
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

---

## Test results

### DACE ON vs OFF — phones (2026-04-16, definitive)
**Config:** superfast preset + ssim tune, zero-latency, ABR, psy off, 40kbps, 3fps, 320×240
**Transport:** WNR + 2M PHY both sides + DLE (MTU=517)
**Source:** `complex_320x240.yuv`

| Mode | SEND | RECV | tput | PSNR avg | PSNR ss | SSIM avg | SSIM ss | NAL avg | Enc avg |
|------|------|------|------|----------|---------|----------|---------|---------|---------|
| DACE ON (cl=-1) | 51 | 10 | 72 kbps | 29.88 dB | 29.35 dB | 0.817 | 0.810 | 1911 B | 13.5 ms |
| DACE OFF (cl=0) | 81 | 11 | 68 kbps | 26.97 dB | 26.57 dB | 0.741 | 0.735 | 1826 B |  4.8 ms |

**Delta: DACE ON +2.9 dB PSNR, +0.076 SSIM, 3× slower encode**
ss = steady-state (skip first 2 frames — IDR spike artificially lowers first-frame PSNR)

### DACE ON vs OFF — Pi5s (2026-04-08, encode-side only)
**Config:** 100kbps, 5fps, 320×240
| Mode | PSNR avg | Enc avg | NAL avg |
|------|----------|---------|---------|
| auto   | 42.7 dB | 46 ms | 2567 B |
| CL0    | 43.4 dB |  6 ms | 2496 B |
| CL1-5  | 43-44 dB | 5-18 ms | 2400-2520 B |

---

## Known issues & gotchas

- **IDR spike**: ABR without VBV — seq 0 ≈22KB, seq 1 ≈11B, then settles. Use steady-state PSNR (NR>2) for fair comparison.
- **MAX_FRAGMENT_SIZE=180**: conservative for old 256B MTU era. MTU is now 517 on phones — could raise to ~460 for 2× delivery improvement (would halve fragment count from ~11 to ~5).
- **RECV variability**: 0-20% across runs — BLE radio + Android 9 ATT stack is the bottleneck.
- **ADB BluetoothMeshService not running**: app may restart between test passes. Always call `am start MainActivity` before each AdbActivity call in scripts, not just once.
- **ADB_CMD logcat delay**: phones need 4-5s after `am start AdbActivity` before log appears.
- **Duplicate BLE connections**: two devices each scanning each other creates 2 connections sharing bandwidth. `isPeerAlreadyConnected()` dedup runs at first-ANNOUNCE time — not instantaneous. Check `Periodic cleanup: N connections` — ideally N=1 per peer.
- **Semaphore(2) HURTS on Android 9**: ATT layer drops 2nd concurrent WNR write → `Semaphore(1)` only.
- **WNR + semaphore is safe**: `onCharacteristicWrite` fires for WNR at stack-enqueue, not after peer ACK. No deadlock.
- **Pi5 clock reset needed**: `adb -s 798f... root && adb shell date -s @$(date +%s)` after reboot.

## User preferences
- Be concise — no summaries, no preamble
- Commit after each meaningful change
- cl=0 = DACE OFF; cl=-1 = DACE auto ON; cl=1-9 = DACE fixed ON
- Reference x264 config is in `~/libtest/test_x264.cpp` — match it
- `PSNR/SSIM` are objective (psy off). IDR spike skews "all-frames" avg — use steady-state
- Use `--src /data/local/tmp/complex_320x240.yuv` for reproducible results
- After stop_client/stop_server, ALWAYS reset debug prefs
- ble_psnr_test.py: `--sender T1AIOC656909KGK --receiver dc1c0ad --bitrate 40000 --fps 3`
