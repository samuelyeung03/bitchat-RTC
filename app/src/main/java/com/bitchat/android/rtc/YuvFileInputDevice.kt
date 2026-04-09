package com.bitchat.android.rtc

import android.util.Log
import com.bitchat.android.util.AppConstants
import java.io.File
import java.io.FileInputStream
import java.io.InputStream

/**
 * Streams planar YUV420 frames from a file at a fixed FPS.
 *
 * The file is treated as a looped frame source: when EOF is reached it restarts from byte 0.
 */
class YuvFileInputDevice(
    private val filePath: String,
    private val width: Int = AppConstants.Dace.DEFAULT_WIDTH,
    private val height: Int = AppConstants.Dace.DEFAULT_HEIGHT,
    private val fps: Int = AppConstants.Dace.DEFAULT_FPS,
    private val onFrame: (yuv420: ByteArray) -> Unit,
) {
    companion object {
        private const val TAG = "YuvFileInputDevice"
    }

    private val frameSize = width * height * 3 / 2

    @Volatile private var running = false
    private var worker: Thread? = null

    fun start(): Boolean {
        if (running) return true

        if (fps <= 0) {
            Log.e(TAG, "Invalid fps=$fps")
            return false
        }
        if (width <= 0 || height <= 0 || width % 2 != 0 || height % 2 != 0) {
            Log.e(TAG, "Invalid size ${width}x${height}; width/height must be positive and even")
            return false
        }

        val file = File(filePath)
        if (!file.exists() || !file.canRead()) {
            Log.e(TAG, "YUV source not readable: $filePath")
            return false
        }
        if (file.length() < frameSize) {
            Log.e(TAG, "YUV source is smaller than one frame: file=${file.length()} frame=$frameSize")
            return false
        }

        running = true
        worker = Thread {
            runLoop(file)
        }.apply {
            name = "YuvFileInputThread"
            isDaemon = true
            start()
        }

        Log.i(TAG, "YUV file input started: $filePath ${width}x${height} @${fps}fps")
        return true
    }

    fun stop() {
        running = false
        worker?.interrupt()
        try {
            worker?.join(500)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
        worker = null
        Log.i(TAG, "YUV file input stopped")
    }

    private fun runLoop(file: File) {
        val frameIntervalNs = 1_000_000_000L / fps
        var input: FileInputStream? = null

        try {
            while (running) {
                if (input == null) {
                    input = FileInputStream(file)
                }

                val frame = ByteArray(frameSize)
                val ok = readExactly(input, frame)
                if (!ok) {
                    input.close()
                    input = null
                    continue
                }

                val t0 = System.nanoTime()
                onFrame(frame)

                val elapsed = System.nanoTime() - t0
                val sleepNs = frameIntervalNs - elapsed
                if (sleepNs > 0 && running) {
                    try {
                        val sleepMs = sleepNs / 1_000_000L
                        val remNs = (sleepNs % 1_000_000L).toInt()
                        Thread.sleep(sleepMs, remNs)
                    } catch (_: InterruptedException) {
                        if (!running) break
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "YUV file loop failed: ${e.message}", e)
        } finally {
            try {
                input?.close()
            } catch (_: Exception) {
            }
        }
    }

    private fun readExactly(input: InputStream, out: ByteArray): Boolean {
        var off = 0
        while (off < out.size && running) {
            val n = input.read(out, off, out.size - off)
            if (n < 0) return false
            off += n
        }
        return off == out.size
    }
}
