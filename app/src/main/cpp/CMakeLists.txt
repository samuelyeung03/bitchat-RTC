cmake_minimum_required(VERSION 3.6)
project(opuswrapper)

add_library(opuswrapper SHARED opus_jni.cpp)

# Make sure to add the include directory for Opus headers:
include_directories(include)

# Link against the prebuilt opus shared library
add_library(opus SHARED IMPORTED)
set_target_properties(opus PROPERTIES IMPORTED_LOCATION
    ${CMAKE_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}/libopus.so)

target_link_libraries(opuswrapper opus log) 