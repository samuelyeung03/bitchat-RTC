cmake_minimum_required(VERSION 3.6)
project(opuswrapper)

# ── Opus (existing audio codec) ───────────────────────────────────────────────

add_library(opuswrapper SHARED opus_jni.cpp)

include_directories(include)

add_library(opus SHARED IMPORTED)
set_target_properties(opus PROPERTIES IMPORTED_LOCATION
    ${CMAKE_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}/libopus.so)

target_link_libraries(opuswrapper opus log)

# ── x264 DACE (Dynamic Adaptive Complexity Encoding video codec) ───────────────

include(ExternalProject)

set(X264_SOURCE_DIR ${CMAKE_SOURCE_DIR}/x264)
set(X264_INSTALL_DIR ${CMAKE_BINARY_DIR}/x264_install)

# Map Android ABI to x264 cross-compile host triple
if(ANDROID_ABI STREQUAL "arm64-v8a")
    set(X264_HOST "aarch64-linux-android")
elseif(ANDROID_ABI STREQUAL "armeabi-v7a")
    set(X264_HOST "arm-linux-androideabi")
elseif(ANDROID_ABI STREQUAL "x86_64")
    set(X264_HOST "x86_64-linux-android")
elseif(ANDROID_ABI STREQUAL "x86")
    set(X264_HOST "i686-linux-android")
else()
    set(X264_HOST "aarch64-linux-android")
endif()

# Resolve NDK sysroot and toolchain paths
set(NDK_TOOLCHAIN_BIN "${ANDROID_TOOLCHAIN_ROOT}/bin")

ExternalProject_Add(x264_dace
    SOURCE_DIR   ${X264_SOURCE_DIR}
    INSTALL_DIR  ${X264_INSTALL_DIR}
    CONFIGURE_COMMAND
        ${CMAKE_COMMAND} -E env
            CC=${NDK_TOOLCHAIN_BIN}/${X264_HOST}${ANDROID_NATIVE_API_LEVEL}-clang
            AR=${NDK_TOOLCHAIN_BIN}/llvm-ar
            RANLIB=${NDK_TOOLCHAIN_BIN}/llvm-ranlib
            STRIP=${NDK_TOOLCHAIN_BIN}/llvm-strip
        ${X264_SOURCE_DIR}/configure
            --host=${X264_HOST}
            --cross-prefix=${NDK_TOOLCHAIN_BIN}/${X264_HOST}-
            --sysroot=${CMAKE_SYSROOT}
            --prefix=${X264_INSTALL_DIR}
            --enable-static
            --disable-shared
            --disable-cli
            --disable-asm
            --disable-opencl
            --disable-thread
            --enable-pic
            --bit-depth=8
            --chroma-format=420
    BUILD_COMMAND   $(MAKE)
    INSTALL_COMMAND $(MAKE) install
    BUILD_IN_SOURCE 0
)

# Import the static x264 library produced by ExternalProject
add_library(x264_static STATIC IMPORTED)
set_target_properties(x264_static PROPERTIES
    IMPORTED_LOCATION ${X264_INSTALL_DIR}/lib/libx264.a)
add_dependencies(x264_static x264_dace)

# DACE JNI shared library
add_library(dacewrapper SHARED dace_jni.cpp)

target_include_directories(dacewrapper PRIVATE
    ${X264_INSTALL_DIR}/include)

target_link_libraries(dacewrapper x264_static log)
add_dependencies(dacewrapper x264_dace)
