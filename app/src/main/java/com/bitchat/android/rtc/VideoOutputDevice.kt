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
    textureView: TextureView? = null,
    private var width: Int  = AppConstants.Dace.DEFAULT_WIDTH,
    private var height: Int = AppConstants.Dace.DEFAULT_HEIGHT
) {
    @Volatile private var textureView: TextureView? = textureView

    fun setTextureView(view: TextureView) {
        textureView = view
    }

    fun setResolution(w: Int, h: Int) {
        width = w
        height = h
    }
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
            val tv = textureView ?: return@post
            if (!tv.isAvailable) return@post
            val canvas = tv.lockCanvas() ?: return@post
            try {
                val bitmap = yuv420ToBitmap(yuv420, width, height)
                val viewWidth = tv.width.toFloat()
                val viewHeight = tv.height.toFloat()
                val scale = minOf(viewWidth / width, viewHeight / height)
                val scaledWidth = width * scale
                val scaledHeight = height * scale
                val left = (viewWidth - scaledWidth) / 2
                val top = (viewHeight - scaledHeight) / 2
                val dst = android.graphics.RectF(left, top, left + scaledWidth, top + scaledHeight)
                canvas.drawBitmap(bitmap, null, dst, null)
                bitmap.recycle()
            } catch (e: Exception) {
                Log.w(TAG, "renderFrame error: ${e.message}")
            } finally {
                tv.unlockCanvasAndPost(canvas)
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
