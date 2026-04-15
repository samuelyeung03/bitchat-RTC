#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage:
  reflash_rpi5_ssd_with_bootfix.sh [--yes] [aosp_root] [target_dev] [nrf_dtbo]

Examples:
  reflash_rpi5_ssd_with_bootfix.sh
  reflash_rpi5_ssd_with_bootfix.sh --yes /media/samuelyeung03/Data/aosp-rpi16 /dev/sdc /home/samuelyeung03/bit-chat/bitchat-RTC/test/nrf-hci.dtbo
  CONFIRM_REFLASH=YES reflash_rpi5_ssd_with_bootfix.sh /media/samuelyeung03/Data/aosp-rpi16 /dev/sdc /home/samuelyeung03/bit-chat/bitchat-RTC/test/nrf-hci.dtbo
EOF
  exit 0
fi

CONFIRM_MODE="${CONFIRM_REFLASH:-}"
if [[ "${1:-}" == "--yes" || "${1:-}" == "CONFIRM_REFLASH=YES" ]]; then
  CONFIRM_MODE="YES"
  shift
fi

AOSP_ROOT="${1:-/media/samuelyeung03/Data/aosp-rpi16}"
TARGET_DEV="${2:-/dev/sdc}"
NRF_DTBO="${3:-/home/samuelyeung03/bit-chat/bitchat-RTC/test/nrf-hci.dtbo}"
PRODUCT_OUT="$AOSP_ROOT/out/target/product/rpi5"
TARGET_PRODUCT_VALUE="${TARGET_PRODUCT_VALUE:-aosp_rpi5-bp4a-userdebug}"

log() {
  echo "[reflash-rpi5] $*"
}

build_full_image() {
  log "Building full RPi5 disk image with current artifacts"
  cd "$AOSP_ROOT"
  export TARGET_PRODUCT="$TARGET_PRODUCT_VALUE"
  export ANDROID_PRODUCT_OUT="$PRODUCT_OUT"
  ./rpi5-mkimg.sh
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    log "Missing command: $1"
    exit 1
  }
}

append_if_missing() {
  local line="$1"
  local file="$2"
  if ! sudo grep -qxF "$line" "$file"; then
    echo "$line" | sudo tee -a "$file" >/dev/null
  fi
}

for cmd in lsblk sudo dd mount umount sync partprobe; do
  require_cmd "$cmd"
done

if [[ ! -d "$AOSP_ROOT" ]]; then
  log "AOSP root not found: $AOSP_ROOT"
  exit 1
fi
if [[ ! -d "$PRODUCT_OUT" ]]; then
  log "Product output not found: $PRODUCT_OUT"
  exit 1
fi
if [[ ! -b "$TARGET_DEV" ]]; then
  log "Target block device not found: $TARGET_DEV"
  exit 1
fi
if [[ "$(lsblk -dn -o TYPE "$TARGET_DEV")" != "disk" ]]; then
  log "Target is not a disk: $TARGET_DEV"
  exit 1
fi
if [[ ! -f "$PRODUCT_OUT/boot.img" || ! -f "$PRODUCT_OUT/system.img" || ! -f "$PRODUCT_OUT/vendor.img" ]]; then
  log "Missing partition images in $PRODUCT_OUT (need boot.img/system.img/vendor.img)"
  exit 1
fi
if [[ ! -f "$NRF_DTBO" ]]; then
  log "nRF overlay not found at $NRF_DTBO"
  log "Proceeding without nRF overlay copy (Bluetooth UART overlay may be missing)."
fi

if [[ "$CONFIRM_MODE" != "YES" ]]; then
  log "About to ERASE and rewrite $TARGET_DEV"
  lsblk -o NAME,SIZE,TYPE,MODEL,SERIAL,TRAN "$TARGET_DEV"
  log "Re-run with: $0 --yes '$AOSP_ROOT' '$TARGET_DEV' '$NRF_DTBO'"
  log "Alternative: CONFIRM_REFLASH=YES $0 '$AOSP_ROOT' '$TARGET_DEV' '$NRF_DTBO'"
  exit 2
fi

IMG_BASENAME="RaspberryVanillaAOSP16-$(date +%Y%m%d)-${TARGET_PRODUCT_VALUE#aosp_}.img"
IMG_PATH="$PRODUCT_OUT/$IMG_BASENAME"
LATEST_PART_TS="$(stat -c %Y "$PRODUCT_OUT/boot.img" "$PRODUCT_OUT/system.img" "$PRODUCT_OUT/vendor.img" | sort -nr | head -n 1)"

if [[ -f "$IMG_PATH" ]]; then
  IMG_TS="$(stat -c %Y "$IMG_PATH")"
  if (( IMG_TS >= LATEST_PART_TS )); then
    log "Reusing existing up-to-date image: $IMG_PATH"
  else
    log "Existing image is stale; rebuilding: $IMG_PATH"
    rm -f "$IMG_PATH"
    build_full_image
  fi
else
  build_full_image
fi

if [[ ! -f "$IMG_PATH" ]]; then
  IMG_PATH="$(ls -1t "$PRODUCT_OUT"/RaspberryVanillaAOSP16-*.img 2>/dev/null | head -n 1 || true)"
fi
if [[ -z "${IMG_PATH:-}" || ! -f "$IMG_PATH" ]]; then
  log "Unable to locate generated full image in $PRODUCT_OUT"
  exit 1
fi
log "Using image: $IMG_PATH"

log "Unmounting existing partitions on $TARGET_DEV"
sudo umount "${TARGET_DEV}"* 2>/dev/null || true

log "Writing image to $TARGET_DEV"
sudo dd if="$IMG_PATH" of="$TARGET_DEV" bs=4M conv=fsync status=progress
sync
sudo partprobe "$TARGET_DEV" || true
sleep 2

BOOT_PART="${TARGET_DEV}1"
if [[ ! -b "$BOOT_PART" ]]; then
  log "Boot partition not found after flash: $BOOT_PART"
  exit 1
fi

BOOT_MNT="$(mktemp -d /tmp/rpi5boot.XXXXXX)"
cleanup() {
  set +e
  mountpoint -q "$BOOT_MNT" && sudo umount "$BOOT_MNT"
  rmdir "$BOOT_MNT" 2>/dev/null || true
}
trap cleanup EXIT

log "Applying boot config fixups for RPi5 SSD + nRF"
sudo mount "$BOOT_PART" "$BOOT_MNT"

if [[ -f "$NRF_DTBO" ]]; then
  sudo install -m 0644 "$NRF_DTBO" "$BOOT_MNT/overlays/nrf-hci.dtbo"
fi

append_if_missing "dtoverlay=android-nvme" "$BOOT_MNT/config.txt"
append_if_missing "dtoverlay=uart0-pi5" "$BOOT_MNT/config.txt"
append_if_missing "dtoverlay=nrf-hci" "$BOOT_MNT/config.txt"
append_if_missing "include config_user.txt" "$BOOT_MNT/config.txt"

if [[ ! -f "$BOOT_MNT/config_user.txt" ]]; then
  sudo touch "$BOOT_MNT/config_user.txt"
fi

log "Final boot config tail"
sudo tail -n 30 "$BOOT_MNT/config.txt"

sync
sudo umount "$BOOT_MNT"
rmdir "$BOOT_MNT"
trap - EXIT

log "Reflash complete for $TARGET_DEV"
log "You can now move SSD back to the Pi 5 and boot."
