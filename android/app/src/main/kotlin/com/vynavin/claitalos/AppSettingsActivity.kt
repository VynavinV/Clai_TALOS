package com.vynavin.claitalos

import android.content.Intent
import android.os.Bundle
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.vynavin.claitalos.databinding.ActivityAppSettingsBinding
import com.vynavin.claitalos.mobile.TalosClient
import kotlinx.coroutines.launch

/**
 * Native app settings: server info, re-pair, connection check, glasses model
 * picker (synced from the server, sent with every message), and the speech
 * toggles used by the ping-back service.
 */
class AppSettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAppSettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityAppSettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as GlassesApp
        binding.serverUrlText.text = app.prefs.serverUrl.ifBlank { "(not paired)" }

        binding.speakRepliesCheck.isChecked = app.prefs.speakReplies
        binding.speakFollowUpsCheck.isChecked = app.prefs.speakFollowUps
        binding.speakRepliesCheck.setOnCheckedChangeListener { _, checked ->
            app.prefs.speakReplies = checked
        }
        binding.speakFollowUpsCheck.setOnCheckedChangeListener { _, checked ->
            app.prefs.speakFollowUps = checked
        }

        binding.checkConnectionButton.setOnClickListener { checkConnection() }
        binding.repairButton.setOnClickListener {
            app.prefs.clear()
            startActivity(
                Intent(this, PairingActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                },
            )
            finish()
        }

        loadGlassesModels()
    }

    private fun loadGlassesModels() {
        val app = application as GlassesApp
        lifecycleScope.launch {
            val client = TalosClient(app.prefs.serverUrl, app.prefs.token)
            when (val result = client.status()) {
                is TalosClient.ApiResult.Ok -> {
                    val models = result.value.glassesModels
                    val adapter = ArrayAdapter(
                        this@AppSettingsActivity,
                        android.R.layout.simple_spinner_dropdown_item,
                        models,
                    )
                    binding.glassesModelSpinner.adapter = adapter
                    val current = app.prefs.glassesModel.ifBlank { result.value.glassesModel }
                    val idx = models.indexOf(current).coerceAtLeast(0)
                    binding.glassesModelSpinner.setSelection(idx)
                    binding.glassesModelSpinner.onItemSelectedListener =
                        object : android.widget.AdapterView.OnItemSelectedListener {
                            override fun onItemSelected(
                                parent: android.widget.AdapterView<*>?,
                                view: android.view.View?,
                                position: Int,
                                id: Long,
                            ) {
                                app.prefs.glassesModel = models[position]
                            }

                            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
                        }
                    binding.settingsStatus.text = "Connected as ${result.value.botName}"
                }
                else -> {
                    val fallback = listOf("Ray-Ban Meta Gen 1", "Ray-Ban Meta Gen 2", "Oakley Meta", "Other")
                    binding.glassesModelSpinner.adapter = ArrayAdapter(
                        this@AppSettingsActivity,
                        android.R.layout.simple_spinner_dropdown_item,
                        fallback,
                    )
                    binding.settingsStatus.text = "Could not load models from server — using defaults."
                }
            }
        }
    }

    private fun checkConnection() {
        val app = application as GlassesApp
        binding.settingsStatus.text = "Checking…"
        lifecycleScope.launch {
            val result = TalosClient(app.prefs.serverUrl, app.prefs.token).status()
            binding.settingsStatus.text = when (result) {
                is TalosClient.ApiResult.Ok ->
                    "Connected. Telegram: ${if (result.value.telegramConfigured) "on" else "off"} · " +
                        "speech: ${if (result.value.speakEnabled) "on" else "off"}"
                is TalosClient.ApiResult.Err.Unauthorized -> "Token rejected — pair again."
                is TalosClient.ApiResult.Err.Network -> "No connection to server."
                else -> "Server error."
            }
        }
    }
}
