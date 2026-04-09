cmake_minimum_required(VERSION 3.6)
project(opuswrapper)

# ── Opus (existing audio codec) ───────────────────────────────────────────────

add_library(opuswrapper SHARED opus_jni.cpp)
include_directories(include)

add_library(opus SHARED IMPORTED)
set_target_properties(opus PROPERTIES IMPORTED_LOCATION
    ${CMAKE_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}/libopus.so)

target_link_libraries(opuswrapper opus log)

# ── x264 DACE — pre-built static library per ABI ──────────────────────────────
# libx264.a lives in prebuilt/<ABI>/ (committed to repo).
# To rebuild: bash test/build_and_push.sh   (rebuilds for arm64-v8a)
# Headers come from the x264 submodule at app/src/main/cpp/x264/

set(X264_PREBUILT "${CMAKE_SOURCE_DIR}/prebuilt/${ANDROID_ABI}/libx264.a")

if(NOT EXISTS "${X264_PREBUILT}")
    message(FATAL_ERROR
        "[DACE] Prebuilt libx264.a not found for ${ANDROID_ABI}: ${X264_PREBUILT}\n"
        "Run:  bash test/build_and_push.sh  (from repo root) to rebuild it.")
endif()

add_library(x264_static STATIC IMPORTED)
set_target_properties(x264_static PROPERTIES IMPORTED_LOCATION "${X264_PREBUILT}")

# DACE JNI shared library
add_library(dacewrapper SHARED dace_jni.cpp)
target_include_directories(dacewrapper PRIVATE "${CMAKE_SOURCE_DIR}/x264")
target_link_libraries(dacewrapper x264_static log)
