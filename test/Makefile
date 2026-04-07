NDK        := $(HOME)/Android/Sdk/ndk/27.2.12479018
TC         := $(NDK)/toolchains/llvm/prebuilt/linux-x86_64/bin
CC         := $(TC)/aarch64-linux-android29-clang
X264       := /tmp/x264-dace-arm64
OUT        := /tmp

CFLAGS     := -O2 -I$(X264)/include
LDFLAGS    := $(X264)/lib/libx264.a -lm -Wl,-z,max-page-size=16384

.PHONY: all bench hw clean

all: bench hw

# Synthetic benchmark (DACE encoding time only, no camera)
bench: dace_bench.c
	$(CC) $(CFLAGS) -o $(OUT)/dace_bench $< $(LDFLAGS)
	@echo "Built $(OUT)/dace_bench"

# Hardware test (V4L2 + DACE encode + TCP + AMediaCodec decode + PSNR)
hw: dace_hw_bench.c
	$(CC) $(CFLAGS) -o $(OUT)/dace_hw_bench $< $(LDFLAGS)
	@echo "Built $(OUT)/dace_hw_bench"

clean:
	rm -f $(OUT)/dace_bench $(OUT)/dace_hw_bench
