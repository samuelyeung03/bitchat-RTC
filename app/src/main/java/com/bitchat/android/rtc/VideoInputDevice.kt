package com.bitchat.android.rtc

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.hardware.camera2.*
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.util.Size
import com.bitchat.android.util.AppConstants

/**
 * Captures camera frames as YUV420_888 and converts them to planar YUV420
 * (the format expected by DACEEncoder).
 *
 * Call [start] to begin capture and [stop] to release all resources.
 * Each captured frame is delivered via [onFrame].
 */
class VideoInputDevice(
    private val context: Context,
    private val width: Int  = AppConstants.Dace.DEFAULT_WIDTH,
    private val height: Int = AppConstants.Dace.DEFAULT_HEIGHT,
    private val fps: Int    = AppConstants.Dace.DEFAULT_FPS,
    private val onFrame: (yuv420: ByteArray) -> Unit
) {
    companion object {
        private const val TAG = "VideoInputDevice"
        private const val MAX_IMAGES = 2
    }

    private val cameraThread  = HandlerThread("CameraThread").also { it.start() }
    private val cameraHandler = Handler(cameraThread.looper)

    // Software frame throttle: drop frames to stay at target fps
    private val minFrameIntervalNs = if (fps > 0) (1_000_000_000L / fps) else 0L
    @Volatile private var lastFrameNs = 0L

    private var cameraDevice:   CameraDevice?     = null
    private var captureSession: CameraCaptureSession? = null
    private var imageReader:    ImageReader?       = null

    @SuppressLint("MissingPermission")
    fun start() {
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager

        // Prefer the front-facing camera for video calls
        val cameraId = manager.cameraIdList.firstOrNull { id ->
            val chars = manager.getCameraCharacteristics(id)
            chars.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_FRONT
        } ?: manager.cameraIdList.firstOrNull() ?: run {
            Log.e(TAG, "No camera available")
            return
        }

        imageReader = ImageReader.newInstance(width, height, ImageFormat.YUV_420_888, MAX_IMAGES)
        imageReader!!.setOnImageAvailableListener({ reader ->
            val image = reader.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                // Software FPS cap: drop frames that arrive too early
                val now = System.nanoTime()
                if (minFrameIntervalNs > 0 && (now - lastFrameNs) < minFrameIntervalNs) {
                    return@setOnImageAvailableListener
                }
                lastFrameNs = now
                val yuv = imageToYuv420(image, width, height)
                onFrame(yuv)
            } finally {
                image.close()
            }
        }, cameraHandler)

        manager.openCamera(cameraId, object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                cameraDevice = camera
                startCaptureSession(camera)
            }
            override fun onDisconnected(camera: CameraDevice) { camera.close() }
            override fun onError(camera: CameraDevice, error: Int) {
                Log.e(TAG, "Camera error $error"); camera.close()
            }
        }, cameraHandler)
    }

    private fun startCaptureSession(camera: CameraDevice) {
        val surface = imageReader!!.surface
        camera.createCaptureSession(listOf(surface), object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(session: CameraCaptureSession) {
                captureSession = session
                val request = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                    addTarget(surface)
                    set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                    // Clamp camera output to target FPS to avoid encoding more frames than BLE can carry
                    set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, android.util.Range(fps, fps))
                }
                session.setRepeatingRequest(request.build(), null, cameraHandler)
                Log.i(TAG, "Camera capture started: ${width}x$height")
            }
            override fun onConfigureFailed(session: CameraCaptureSession) {
                Log.e(TAG, "CaptureSession configure failed")
            }
        }, cameraHandler)
    }

    fun stop() {
        try { captureSession?.stopRepeating() } catch (_: Exception) {}
        captureSession?.close()
        cameraDevice?.close()
        imageReader?.close()
        cameraThread.quitSafely()
        captureSession = null
        cameraDevice   = null
        imageReader    = null
        Log.i(TAG, "Camera capture stopped")
    }

    // Convert android.media.Image (YUV_420_888) to contiguous planar YUV420 ByteArray
    private fun imageToYuv420(image: android.media.Image, w: Int, h: Int): ByteArray {
        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        val yBuf = yPlane.buffer
        val uBuf = uPlane.buffer
        val vBuf = vPlane.buffer

        val ySize = w * h
        val uvSize = (w / 2) * (h / 2)
        val out = ByteArray(ySize + uvSize * 2)

        // Copy Y plane
        yBuf.get(out, 0, ySize)

        // Copy U and V planes (handle potential interleaving via pixel stride)
        val uPixelStride = uPlane.pixelStride
        val vPixelStride = vPlane.pixelStride
        val uRowStride   = uPlane.rowStride
        val vRowStride   = vPlane.rowStride

        var uOff = ySize
        var vOff = ySize + uvSize
        for (row in 0 until h / 2) {
            for (col in 0 until w / 2) {
                out[uOff++] = uBuf.get(row * uRowStride + col * uPixelStride)
                out[vOff++] = vBuf.get(row * vRowStride + col * vPixelStride)
            }
        }
        return out
    }
}
