package com.vynavin.claitalos

import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Log
import android.view.MotionEvent
import android.widget.ImageButton
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.core.content.ContextCompat.getMainExecutor
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import com.vynavin.claitalos.databinding.ActivityQrScanBinding
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * QR scanner built on CameraX + ML Kit (replaces zxing-android-embedded,
 * whose legacy android.hardware.Camera backend has broken autofocus and
 * auto-exposure on modern devices, and locked orientation to landscape).
 *
 * CameraX gives continuous autofocus + auto exposure; tapping the preview
 * triggers a focus/metering pull at that point. Portrait, torch toggle.
 * Returns the decoded text as `qr` in the result Intent.
 */
class QrScanActivity : AppCompatActivity() {

    private lateinit var binding: ActivityQrScanBinding
    private var camera: Camera? = null
    private var torchOn = false
    private val delivered = AtomicBoolean(false)
    private val analysisExecutor = Executors.newSingleThreadExecutor()

    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build(),
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityQrScanBinding.inflate(layoutInflater)
        setContentView(binding.root)

        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) {
            Toast.makeText(this, "Camera permission is required.", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        binding.torchButton.setOnClickListener {
            torchOn = !torchOn
            camera?.cameraControl?.enableTorch(torchOn)
            binding.torchButton.alpha = if (torchOn) 1f else 0.55f
        }
        binding.closeButton.setOnClickListener { finish() }

        binding.previewView.post { startCamera() }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()

            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.previewView.surfaceProvider)
            }

            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
            analysis.setAnalyzer(analysisExecutor, ::analyzeFrame)

            try {
                provider.unbindAll()
                camera = provider.bindToLifecycle(
                    this,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    analysis,
                ).apply {
                    // Continuous AF/AE is CameraX's default; tap-to-focus below
                    // adds a one-shot pull exactly where the user tapped.
                }
            } catch (e: Exception) {
                Log.e(TAG, "Camera bind failed", e)
                Toast.makeText(this, "Could not start the camera.", Toast.LENGTH_SHORT).show()
                finish()
            }
        }, getMainExecutor(this))

        // Tap-to-focus / exposure at the tapped point.
        binding.previewView.setOnTouchListener { view, event ->
            if (event.action == MotionEvent.ACTION_DOWN) {
                val cam = camera ?: return@setOnTouchListener false
                val factory = binding.previewView.meteringPointFactory
                val point = factory.createPoint(event.x, event.y)
                cam.cameraControl.startFocusAndMetering(
                    FocusMeteringAction.Builder(
                        point,
                        FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE,
                    ).build(),
                )
                view.performClick()
            }
            true
        }
    }

    @OptIn(ExperimentalGetImage::class)
    private fun analyzeFrame(proxy: ImageProxy) {
        if (delivered.get()) {
            proxy.close()
            return
        }
        val mediaImage = proxy.image
        if (mediaImage == null) {
            proxy.close()
            return
        }
        val input = InputImage.fromMediaImage(mediaImage, proxy.imageInfo.rotationDegrees)
        scanner.process(input)
            .addOnSuccessListener { barcodes ->
                val value = barcodes.firstOrNull()?.rawValue
                if (!value.isNullOrBlank() && delivered.compareAndSet(false, true)) {
                    runOnUiThread { deliver(value) }
                }
            }
            .addOnCompleteListener { proxy.close() }
    }

    private fun deliver(value: String) {
        setResult(RESULT_OK, Intent().putExtra(EXTRA_QR, value))
        finish()
    }

    override fun onDestroy() {
        analysisExecutor.shutdown()
        scanner.close()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ClaiQrScan"
        const val EXTRA_QR = "qr"
    }
}
