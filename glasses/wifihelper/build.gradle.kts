import com.android.build.api.dsl.ApplicationExtension

// "Wi-Fi 켜기 도우미": turns Wi-Fi on for the main app. Android lets only apps that target
// API 28 or lower call WifiManager.setWifiEnabled (newer targets get a silent "false"), and the
// main app cannot target 28: silent OTA updates need a newer target. So this tiny app does
// nothing else, has no screen, and is installed once.
plugins {
    alias(libs.plugins.android.application)
}

val releaseKeystore = providers.gradleProperty("RELEASE_KEYSTORE").orNull
val releaseKeystorePass = providers.gradleProperty("RELEASE_KEYSTORE_PASS").orNull

kotlin {
    jvmToolchain(21)
}

configure<ApplicationExtension> {
    namespace = "com.chorockkim.conversate.wifihelper"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.chorockkim.conversate.wifihelper"
        minSdk = 26
        @Suppress("ExpiredTargetSdkVersion")
        targetSdk = 28 // on purpose, see above
        versionCode = 2
        versionName = "1.1"
    }

    signingConfigs {
        if (releaseKeystore != null && releaseKeystorePass != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = releaseKeystorePass
                keyAlias = "conversate"
                keyPassword = releaseKeystorePass
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    lint {
        checkReleaseBuilds = false
        disable += "ExpiredTargetSdkVersion"
    }
}
