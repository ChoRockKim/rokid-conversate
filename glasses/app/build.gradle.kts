import com.android.build.api.dsl.ApplicationExtension
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.paparazzi)
}

// Default server address and token baked into the APK, from local.properties, -P properties, or
// the environment. Both can be changed later on the glasses (see README), so these are optional.
val localProps = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}
fun setting(name: String, env: String): String =
    (localProps.getProperty(name) ?: providers.gradleProperty(name).orNull
        ?: providers.environmentVariable(env).orNull ?: "").trim().replace("\"", "")

// Fixed release signing key: OTA updates only install over an APK signed with the same key.
// Never in the repository; passed at build time (scripts/release.sh does this):
//   ./gradlew assembleRelease -PRELEASE_KEYSTORE=/path/release.keystore -PRELEASE_KEYSTORE_PASS=…
val releaseKeystore = providers.gradleProperty("RELEASE_KEYSTORE").orNull
val releaseKeystorePass = providers.gradleProperty("RELEASE_KEYSTORE_PASS").orNull

kotlin {
    jvmToolchain(21)
}

configure<ApplicationExtension> {
    namespace = "com.chorockkim.conversate"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.chorockkim.conversate"
        // Rokid Glasses run Android 12 (API 31/32).
        minSdk = 26
        targetSdk = 35
        // Bumped per release by scripts/release.sh (-PVERSION_CODE / -PVERSION_NAME).
        versionCode = providers.gradleProperty("VERSION_CODE").orNull?.toInt() ?: 1
        versionName = providers.gradleProperty("VERSION_NAME").orNull ?: "0.1.0"

        buildConfigField("String", "DEFAULT_SERVER_URL", "\"${setting("conversateUrl", "CONVERSATE_URL")}\"")
        buildConfigField("String", "DEFAULT_TOKEN", "\"${setting("conversateToken", "CONVERSATE_TOKEN")}\"")
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
            // Without the release key this falls back to the debug key (fine for local tests,
            // but such an APK cannot update one signed with the release key, or vice versa).
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    lint {
        // lintVital on release builds downloads its own toolchain, which breaks scripted releases
        // when Maven is flaky; unit tests and debug builds cover what we need.
        checkReleaseBuilds = false
    }

    testOptions {
        unitTests.isIncludeAndroidResources = true
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation(libs.androidx.core)
    implementation(libs.okhttp)

    testImplementation(libs.junit)
    // Real org.json for JVM unit tests (android.jar only has stubs).
    testImplementation(libs.json)
    testImplementation(libs.okhttp.mockwebserver)
}

tasks.withType<Test>().configureEach {
    testLogging { showStandardStreams = true }
}
