package com.vynavin.claitalos

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.vynavin.claitalos.databinding.ActivityPairingBinding
import com.vynavin.claitalos.mobile.PairingPayload
import com.vynavin.claitalos.mobile.TalosClient
import kotlinx.coroutines.launch

/**
 * Onboarding: scan the dashboard pairing QR (server URL + token + glasses
 * model), or paste both manually. Verifies via GET /api/mobile/status before
 * saving; stores everything in EncryptedSharedPreferences.
 */
class PairingActivity : AppCompatActivity() {

    private lateinit var binding: ActivityPairingBinding

    private val qrScanner = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val contents = result.data?.getStringExtra(QrScanActivity.EXTRA_QR)
        if (!contents.isNullOrBlank()) {
            applyPayload(contents)
        }
    }

    private val cameraPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) launchScanner() else {
            Toast.makeText(this, "Camera permission needed to scan QR codes.", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityPairingBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Handle the claitalos:// callback URI scheme (Meta AI app return).
        intent?.data?.toString()?.let { applyPayload(it) }

        binding.scanQrButton.setOnClickListener {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED
            ) {
                launchScanner()
            } else {
                cameraPermission.launch(Manifest.permission.CAMERA)
            }
        }

        binding.connectButton.setOnClickListener { connect() }
    }

    private fun launchScanner() {
        qrScanner.launch(Intent(this, QrScanActivity::class.java))
    }

    private fun applyPayload(raw: String) {
        val parsed = PairingPayload.parse(raw)
        if (parsed.serverUrl.isNotBlank()) binding.serverUrl.setText(parsed.serverUrl)
        if (parsed.token.isNotBlank()) binding.token.setText(parsed.token)
        parsed.glassesModel?.let {
            (application as GlassesApp).prefs.glassesModel = it
        }
        if (parsed.isValid) {
            binding.pairingStatus.text = "QR scanned — tap Connect."
        }
    }

    private fun connect() {
        val url = binding.serverUrl.text.toString().trim().trimEnd('/')
        val token = binding.token.text.toString().trim()
        if (url.isBlank() || token.isBlank()) {
            binding.pairingStatus.text = "Enter the server URL and token (or scan the QR)."
            return
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            binding.pairingStatus.text = "URL must start with http:// or https://"
            return
        }
        if (url.startsWith("http://") && !url.substringAfter("http://").substringBefore(':').let {
                it == "localhost" || it == "127.0.0.1" || it.matches(Regex("^192\\.168\\..*")) ||
                    it.matches(Regex("^10\\..*")) || it.matches(Regex("^172\\.(1[6-9]|2[0-9]|3[01])\\..*"))
            }
        ) {
            MaterialAlertDialogBuilder(this)
                .setTitle("Unencrypted connection")
                .setMessage(
                    "This server URL uses plain HTTP outside your LAN. Your token and messages " +
                        "would travel unencrypted. Continue anyway?",
                )
                .setPositiveButton("Continue") { _, _ -> verify(url, token) }
                .setNegativeButton("Cancel", null)
                .show()
            return
        }
        verify(url, token)
    }

    private fun verify(url: String, token: String) {
        binding.pairingStatus.text = "Checking server…"
        binding.connectButton.isEnabled = false
        lifecycleScope.launch {
            when (val result = TalosClient(url, token).status()) {
                is TalosClient.ApiResult.Ok -> {
                    val prefs = (application as GlassesApp).prefs
                    prefs.serverUrl = url
                    prefs.token = token
                    if (prefs.glassesModel.isBlank()) {
                        prefs.glassesModel = result.value.glassesModel
                    }
                    prefs.eventCursor = 0L
                    binding.pairingStatus.text = "Connected to ${result.value.botName}!"
                    startActivity(
                        Intent(this@PairingActivity, MainActivity::class.java).apply {
                            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                        },
                    )
                    finish()
                }
                is TalosClient.ApiResult.Err.Unauthorized -> {
                    binding.pairingStatus.text = "Token rejected — create a new one on the dashboard."
                }
                is TalosClient.ApiResult.Err.HttpsRequired -> {
                    binding.pairingStatus.text =
                        "Server requires HTTPS off-LAN. Use your Tailscale https:// URL."
                }
                is TalosClient.ApiResult.Err.Network -> {
                    binding.pairingStatus.text =
                        "Could not reach the server. Check the URL and your connection."
                }
                is TalosClient.ApiResult.Err.RateLimited -> {
                    binding.pairingStatus.text = "Server rate-limited us — wait a moment and retry."
                }
                is TalosClient.ApiResult.Err.NotFound,
                is TalosClient.ApiResult.Err.Server,
                -> {
                    binding.pairingStatus.text = "Server error. Is this a TALOS dashboard URL?"
                }
            }
            binding.connectButton.isEnabled = true
        }
    }
}
