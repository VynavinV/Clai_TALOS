# Clai TALOS — Android App & Glasses

Say **"Hey Meta, start Clai"** on your glasses, speak your request for as long
as you like, then **tap the touchpad on the glasses to send**. Clai runs it
through the normal pipeline — the reply lands on Telegram and in the app as
always — and when a long job finishes (even 20 minutes later), the answer is
**spoken in plain English through the glasses**.

The app is a WebView of your existing TALOS dashboard plus a native Meta DAT
layer (voice invocations, glasses speech recognition, touchpad input, TTS).

## Grandma setup (step by step)

### 1. Your server

Your Clai TALOS server must be running and reachable from your phone:

- On your home Wi-Fi: `http://<server-ip>:8080` works.
- Away from home: use your Tailscale HTTPS URL, e.g. `https://clai.your-tailnet.ts.net`
  (recommended — the mobile API requires HTTPS off-LAN).

### 2. Pair the server (once)

1. Open the dashboard → **Settings** → **📱 Mobile App & Glasses**.
2. Pick your **glasses model**.
3. Type a device name (e.g. "My glasses") → **Pair New Device**.
4. A QR code appears. Keep this page open.

### 3. Prepare the phone

1. Install this app on your phone (Android 10+):
   - In Android Studio: open the `android/` folder and press Run, or
   - Build an APK: `./gradlew assembleDebug` → `app/build/outputs/apk/debug/app-debug.apk`.
2. Install the **Meta AI app** from the Play Store and pair your glasses with it.
3. Enable **Developer Mode** in the Meta AI app: Settings → App Info → tap
   the app version **five times**.

### 4. Pair the app

1. Open the Clai app → it shows the pairing screen.
2. Tap **Scan QR Code** and scan the QR from step 2 (or paste the URL + token).
3. "Connected to Clai!" — you're paired. The dashboard login is the same one
   you use in the browser.

### 5. Connect the glasses (once)

1. In the Clai app, tap the menu button (bottom-right ⋮) → **Glasses**.
2. Tap **Register with Meta AI** and follow the prompts on the phone.
3. Tap **Allow Glasses Microphone** → approve in the Meta AI app.
4. Tap **Start Glasses Session** → wait for `Session: CONNECTED`.
5. Tap **Test Tap**, then tap the touchpad on your glasses — it should say
   "Tap detected as Select". (If your model maps it differently, tell the
   developer — an alternate stop gesture can be configured.)

### 6. Talk to Clai

1. Put on the glasses and say: **"Hey Meta, start Clai"**.
2. The Clai app opens with a "Listening…" screen. Speak freely — pauses are
   fine, Clai keeps listening.
3. **Tap the touchpad** on the glasses when you're done. That sends it.
4. "Clai is working…" — you can lock the phone. The app keeps a connection
   to your server open.
5. When Clai finishes (minutes later), it **speaks the answer through the
   glasses**. If the glasses weren't connected at that moment, you get a
   phone notification instead and the message replays when you reconnect.

Everything still arrives on Telegram and every other connected channel —
speech through the glasses is purely additional.

## Mock mode (no glasses needed)

On the Glasses screen, check **Use mock glasses** to run the whole flow with
simulated glasses: **Simulate "Hey Meta, start Clai"** drives the voice
invocation, transcriptions can be injected, and a touchpad select stops
dictation. The menu button also has **Demo ping-back (60s)** to hear a spoken
reply arrive a minute later through the real TTS pipeline.

## Project layout

```
android/
├── app/src/main/kotlin/com/vynavin/claitalos/
│   ├── GlassesApp.kt              # Application: Wearables.initialize, voice invocation stream
│   ├── MainActivity.kt            # WebView dashboard + voice invocation intents
│   ├── PairingActivity.kt         # QR/manual pairing → EncryptedSharedPreferences
│   ├── AppSettingsActivity.kt     # Server info, glasses model, speech toggles
│   ├── GlassesStatusActivity.kt   # Register, session, mic permission, Test tap, mock toggle
│   ├── VoiceSessionActivity.kt    # Listening screen (tap-to-stop) → POST → ping-back
│   ├── dat/GlassesConnection.kt   # DeviceSession lifecycle (recreate after STOPPED)
│   ├── dat/DictationSession.kt    # Tap-to-stop dictation + DictationBuffer
│   ├── dat/VoiceInvocationCoordinator.kt  # "Hey Meta, start Clai"
│   ├── mobile/…                   # Prefs, PairingPayload, TalosClient + EventsCursor
│   ├── service/PingbackService.kt # Long-run bridge: long-poll + TTS + notifications
│   ├── service/SpeechOut.kt       # TTS over BT SCO/HFP
│   └── mock/MockMode.kt           # MockDeviceKit helpers
└── app/src/test/…                 # Pairing payload, dictation buffer, cursor, API contract
```

## Building

Requirements: Android Studio (Flamingo or newer) or a local Android SDK, JDK 17.

```
cd android
./gradlew assembleDebug   # build APK
./gradlew lint            # lint (clean)
./gradlew test            # unit tests (14 tests)
```

## Wearables Developer Center (only for publishing / non-Developer-Mode)

Personal Developer-Mode use needs no Dev Center setup: the manifest
placeholders `mwdat_application_id` / `mwdat_client_token` are `"0"`. To go
beyond Developer Mode: create an organization + project at
<https://wearables.developer.meta.com>, register package
`com.vynavin.claitalos`, request **Voice Invocation** approval with spoken
name **"Clai"**, and enable the **Speech** and **Inputs** capabilities.
Replace the placeholders with your APPLICATION_ID and CLIENT_TOKEN.
Voice Invocations, Speech, and Inputs are experimental (dev/beta channels
only) — fine for personal use, not for production release channels.

## Troubleshooting

- **"Token rejected"** — the token was revoked or mistyped. Create a new one
  in Settings → Mobile App & Glasses and re-pair.
- **"HTTPS required"** — you used an `http://` URL off-LAN. Use your
  Tailscale URL, or set `MOBILE_ALLOW_HTTP=1` on a trusted LAN.
- **"Hey Meta" doesn't start Clai** — check Developer Mode is on, the glasses
  are paired to the phone, and (outside Developer Mode) that Voice
  Invocations are approved for the app in the Dev Center.
- **No spoken answer** — the reply still arrives on Telegram. Check the Clai
  persistent notification is present ("Clai is working…"), the glasses show
  `Session: CONNECTED` on the Glasses screen, and phone battery optimization
  isn't killing the app.
- **Tap doesn't stop listening** — run **Test Tap** on the Glasses screen;
  if your model maps the touchpad to something other than `Select`, an
  alternate stop gesture (Nav/Back) can be configured in `DictationSession`.
