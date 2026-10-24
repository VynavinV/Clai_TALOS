package com.vynavin.claitalos

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import com.vynavin.claitalos.databinding.ActivityMainBinding

/**
 * Mobile-optimized WebView of the existing dashboard. The normal browser
 * login applies (session cookie stays in the WebView cookie jar), file
 * uploads work, external links open in the browser, back navigates history.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    companion object {
        private const val FILE_CHOOSER_REQUEST = 51001
    }

    private var fileUploadCallback: ValueCallback<Array<Uri>>? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val app = application as GlassesApp
        if (!app.prefs.isPaired()) {
            startActivity(Intent(this, PairingActivity::class.java))
            finish()
            return
        }

        // "Hey Meta, start Clai" cold/warm launch detection (onCreate path).
        (application as GlassesApp).voiceInvocations.handleIntent(intent)

        binding.nativeMenuButton.setOnClickListener {
            val options = arrayOf("Glasses (register, session, test tap)", "App settings", "Demo ping-back (60s)")
            androidx.appcompat.app.AlertDialog.Builder(this)
                .setTitle("Clai")
                .setItems(options) { _, which ->
                    when (which) {
                        0 -> startActivity(Intent(this, GlassesStatusActivity::class.java))
                        1 -> startActivity(Intent(this, AppSettingsActivity::class.java))
                        2 -> {
                            com.vynavin.claitalos.service.PingbackService.startDemo(this, 60)
                            android.widget.Toast.makeText(
                                this, "Demo reply will be spoken in 60s.", android.widget.Toast.LENGTH_SHORT,
                            ).show()
                        }
                    }
                }
                .show()
        }

        setupWebView()
        binding.webview.loadUrl(app.prefs.serverUrl)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        (application as GlassesApp).voiceInvocations.handleIntent(intent)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        binding.webview.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            setSupportZoom(true)
            builtInZoomControls = true
            displayZoomControls = false
            mediaPlaybackRequiresUserGesture = false
        }
        binding.webview.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                val base = (application as GlassesApp).prefs.serverUrl
                val hostMatches = url.host != null && (
                    url.host == Uri.parse(base).host ||
                        url.host == "localhost" || url.host == "127.0.0.1"
                    )
                return if (!hostMatches && (url.scheme == "http" || url.scheme == "https")) {
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                    true
                } else {
                    false
                }
            }
        }
        binding.webview.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                binding.progress.visibility = if (newProgress in 1..99) View.VISIBLE else View.GONE
                binding.progress.progress = newProgress
            }

            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams,
            ): Boolean {
                fileUploadCallback?.onReceiveValue(null)
                fileUploadCallback = filePathCallback
                val intent = fileChooserParams.createIntent()
                try {
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST)
                } catch (_: Exception) {
                    fileUploadCallback = null
                    return false
                }
                return true
            }
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == FILE_CHOOSER_REQUEST) {
            val callback = fileUploadCallback ?: return super.onActivityResult(requestCode, resultCode, data)
            fileUploadCallback = null
            val uris = WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            callback.onReceiveValue(uris)
            return
        }
        super.onActivityResult(requestCode, resultCode, data)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (binding.webview.canGoBack()) {
            binding.webview.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
