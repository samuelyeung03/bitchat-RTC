package com.bitchat.android.rtc

import android.graphics.Bitmap
import android.graphics.ImageFormat
import android.graphics.SurfaceTexture
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import android.view.TextureView
import com.bitchat.android.util.AppConstants

/**
 * Renders decoded YUV420 frames onto a [TextureView].
 *
 * Usage:
 *   1. Attach the [TextureView] the user wants to display remote video on.
 *   2. Call [renderFrame] from the DACEDecoder callback with each decoded frame.
 *   3. Call [release] when the call ends.
 */
class VideoOutputDevice(
    private val textureView: TextureView,
    private val width: Int  = AppConstants.Dace.DEFAULT_WIDTH,
    private val height: Int = AppConstants.Dace.DEFAULT_HEIGHT
) {
    companion object {
        private const val TAG = "VideoOutputDevice"
    }

    private val renderThread  = HandlerThread("VideoRenderThread").also { it.start() }
    private val renderHandler = Handler(renderThread.looper)

    /**
     * Render one YUV420 planar frame.
     * Safe to call from any thread; rendering is dispatched to [renderHandler].
     */
    fun renderFrame(yuv420: ByteArray) {
        renderHandler.post {
            if (!textureView.isAvailable) return@post
            val canvas = textureView.lockCanvas() ?: return@post
            try {
                val bitmap = yuv420ToBitmap(yuv420, width, height)
                canvas.drawBitmap(bitmap, 0f, 0f, null)
                bitmap.recycle()
            } catch (e: Exception) {
                Log.w(TAG, "renderFrame error: ${e.message}")
            } finally {
                textureView.unlockCanvasAndPost(canvas)
            }
        }
    }

    fun release() {
        renderThread.quitSafely()
        Log.i(TAG, "VideoOutputDevice released")
    }

    // Minimal YUV420 → ARGB_8888 Bitmap conversion via Android's YuvImage helper
    private fun yuv420ToBitmap(yuv420: ByteArray, w: Int, h: Int): Bitmap {
        val yuvImage = android.graphics.YuvImage(
            yuv420, ImageFormat.NV21, w, h, null
        )
        val out = java.io.ByteArrayOutputStream()
        yuvImage.compressToJpeg(android.graphics.Rect(0, 0, w, h), 85, out)
        val jpegBytes = out.toByteArray()
        return android.graphics.BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
    }
}
