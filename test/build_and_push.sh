#!/bin/bash
# build_and_push.sh — rebuild dace_hw_bench and push to both Pi 5s.
# Run from the repo root: bash test/build_and_push.sh
set -e

SENDER="798f51f064cce0d1"
RECEIVER="f501a6221ec14252"
X264_INSTALL="/tmp/x264-dace-arm64"
NDK="$HOME/Android/Sdk/ndk/27.2.12479018"
TC="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"
X264_SRC="app/src/main/cpp/x264"

# Rebuild x264 if missing
if [ ! -f "$X264_INSTALL/lib/libx264.a" ]; then
  echo "[1/3] Building x264 DACE static library …"
  mkdir -p "$X264_INSTALL"
  (cd "$X264_SRC" && \
    CC="$TC/aarch64-linux-android26-clang" \
    AR="$TC/llvm-ar" RANLIB="$TC/llvm-ranlib" \
    ./configure --host=aarch64-linux-android \
      --sysroot="$NDK/toolchains/llvm/prebuilt/linux-x86_64/sysroot" \
      --prefix="$X264_INSTALL" --enable-static --disable-shared \
      --disable-cli --disable-asm --disable-opencl --disable-thread \
      --enable-pic --bit-depth=8 --chroma-format=420 && \
    make -j"$(nproc)" install)
else
  echo "[1/3] x264 already built — skipping"
fi

# Build bench binary
echo "[2/3] Building dace_hw_bench …"
(cd test && make hw)

# Push to both devices
echo "[3/3] Pushing to $SENDER and $RECEIVER …"
adb -s "$SENDER"   push /tmp/dace_hw_bench /data/local/tmp/dace_hw_bench
adb -s "$RECEIVER" push /tmp/dace_hw_bench /data/local/tmp/dace_hw_bench
adb -s "$SENDER"   shell chmod +x /data/local/tmp/dace_hw_bench
adb -s "$RECEIVER" shell chmod +x /data/local/tmp/dace_hw_bench

echo "Done."
