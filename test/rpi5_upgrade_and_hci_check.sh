#!/usr/bin/env bash
set -euo pipefail

SERIAL="${1:-}"
AOSP_ROOT="${2:-/media/samuelyeung03/Data/aosp-rpi16}"
APK_PATH="${3:-/home/samuelyeung03/bit-chat/bitchat-RTC/app/build/outputs/apk/debug/app-debug.apk}"
PRODUCT_OUT="$AOSP_ROOT/out/target/product/rpi5"

if [[ -z "$SERIAL" ]]; then
  echo "Usage: $0 <adb_serial> [aosp_root] [apk_path]"
  exit 1
fi

ADB=(adb -s "$SERIAL")
PKG="com.bitchat.droid"
ACTIVITY="com.bitchat.android.MainActivity"
VENDOR_APEX="$AOSP_ROOT/out/target/product/rpi5/vendor/apex/com.android.hardware.bluetooth.rpi.apex"
BRIDGE_LOCAL="/home/samuelyeung03/bit-chat/bitchat-RTC/test/hci_vhci_bridge"
BRIDGE_REMOTE="/data/local/tmp/hci_vhci_bridge"

log() {
  echo "[rpi5-upgrade] $*"
}

adb_s() {
  "${ADB[@]}" "$@"
}

wait_for_android() {
  local tries=0
  adb_s wait-for-device
  until [[ "$(adb_s shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == "1" ]]; do
    tries=$((tries + 1))
    if (( tries > 120 )); then
      log "Timed out waiting for Android boot completion"
      return 1
    fi
    sleep 2
  done
}

grant_bitchat_perms() {
  local perms=(
    android.permission.BLUETOOTH_ADVERTISE
    android.permission.BLUETOOTH_CONNECT
    android.permission.BLUETOOTH_SCAN
    android.permission.ACCESS_FINE_LOCATION
    android.permission.ACCESS_COARSE_LOCATION
    android.permission.RECORD_AUDIO
    android.permission.CAMERA
    android.permission.POST_NOTIFICATIONS
  )
  local p
  for p in "${perms[@]}"; do
    adb_s shell pm grant "$PKG" "$p" >/dev/null 2>&1 || true
  done
}

if [[ ! -f "$VENDOR_APEX" ]]; then
  log "Missing Bluetooth APEX: $VENDOR_APEX"
  exit 1
fi
if [[ ! -d "$PRODUCT_OUT" ]]; then
  log "Missing product output directory: $PRODUCT_OUT"
  exit 1
fi
if [[ ! -f "$APK_PATH" ]]; then
  log "Missing APK: $APK_PATH"
  exit 1
fi
if ! adb devices | awk 'NR>1 {print $1}' | grep -qx "$SERIAL"; then
  log "Target serial not visible in adb devices: $SERIAL"
  exit 1
fi

log "Acquiring root and remounting"
adb_s root >/dev/null 2>&1 || true
adb_s wait-for-device
adb_s remount >/dev/null 2>&1 || true

# adb sync needs this to know which local images/files to compare and push.
export ANDROID_PRODUCT_OUT="$PRODUCT_OUT"

log "Syncing system partition"
adb_s sync system

log "Syncing vendor partition (fallback to Bluetooth APEX push if full sync fails)"
if ! adb_s sync vendor; then
  log "Full vendor sync failed; pushing only Bluetooth HAL APEX"
  adb_s root >/dev/null 2>&1 || true
  adb_s wait-for-device
  adb_s remount >/dev/null 2>&1 || true
  adb_s push "$VENDOR_APEX" /vendor/apex/com.android.hardware.bluetooth.rpi.apex
fi

log "Rebooting and waiting for Android"
adb_s reboot
wait_for_android

log "Installing BitChat APK"
adb_s install -r "$APK_PATH"
grant_bitchat_perms

log "Restarting Bluetooth service and collecting baseline state"
adb_s shell cmd bluetooth_manager disable >/dev/null 2>&1 || adb_s shell svc bluetooth disable >/dev/null 2>&1 || true
sleep 2
adb_s shell cmd bluetooth_manager enable >/dev/null 2>&1 || adb_s shell svc bluetooth enable >/dev/null 2>&1 || true
sleep 4

log "Bluetooth manager summary"
adb_s shell dumpsys bluetooth_manager | grep -E "enabled:|state:" || true

log "Kernel HCI devices"
adb_s shell ls -1 /sys/class/bluetooth 2>/dev/null || true

if ! adb_s shell test -d /sys/class/bluetooth/hci0; then
  if [[ -f "$BRIDGE_LOCAL" ]]; then
    log "hci0 missing; attempting VHCI bridge fallback"
    adb_s push "$BRIDGE_LOCAL" "$BRIDGE_REMOTE"
    adb_s shell chmod +x "$BRIDGE_REMOTE"
    adb_s shell setenforce 0 >/dev/null 2>&1 || true
    adb_s shell svc bluetooth disable >/dev/null 2>&1 || true
    adb_s shell pkill -f hci_vhci_bridge >/dev/null 2>&1 || true
    adb_s shell "nohup $BRIDGE_REMOTE /dev/ttyAMA10 115200 > /data/local/tmp/hci_vhci_bridge.log 2>&1 &"
    sleep 3
    adb_s shell svc bluetooth enable >/dev/null 2>&1 || true
    sleep 4
    adb_s shell ls -1 /sys/class/bluetooth 2>/dev/null || true
    log "VHCI bridge tail"
    adb_s shell tail -n 40 /data/local/tmp/hci_vhci_bridge.log || true
  else
    log "hci0 missing and bridge binary not found at $BRIDGE_LOCAL"
  fi
fi

log "Launching BitChat"
adb_s shell am start -n "$PKG/$ACTIVITY" >/dev/null 2>&1 || true
sleep 2

log "Bluetooth crash/HAL signatures (if any)"
adb_s shell logcat -d -b all | grep -E "IBluetoothHci::fromBinder|AIBinder_associateClass|FATAL EXCEPTION|Bluetooth crashed|HCIUARTSETPROTO|Protocol not supported" | tail -n 120 || true

log "Done"
