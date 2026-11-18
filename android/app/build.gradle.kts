plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace = "com.vynavin.claitalos"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.vynavin.claitalos"
        minSdk = 29
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

        // Wearables Developer Center attestation for com.vynavin.claitalos.
        // "0"/"0" is the Developer-Mode-only value; these are the real
        // credentials from wearables.developer.meta.com (voice invocation
        // spoken name: "Clai"). They ship inside the APK by design.
        manifestPlaceholders["mwdat_application_id"] = "3616343835217280"
        manifestPlaceholders["mwdat_client_token"] = "AR|3616343835217280|6363fb61fcce4ee94ad86937aabae113"
    }

    signingConfigs {
        // Release-channel builds must carry the SAME signature that is
        // registered in the Wearables Developer Center. We registered the
        // debug keystore's cert, so release builds sign with it too.
        create("channel") {
            storeFile = rootProject.file(System.getProperty("user.home") + "/.android/debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("channel")
        }
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions {
        unitTests {
            isReturnDefaultValues = true
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
    implementation(libs.androidx.activity)
    implementation(libs.androidx.security.crypto)
    implementation(libs.kotlinx.coroutines.android)

    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)
    implementation(libs.mlkit.barcode)

    implementation(libs.mwdat.core)
    implementation(libs.mwdat.speech)
    implementation(libs.mwdat.inputs)
    implementation(libs.mwdat.mockdevice)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.org.json)
}
