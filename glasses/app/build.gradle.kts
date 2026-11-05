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
        versionCode = 1
        versionName = "0.1.0"

        buildConfigField("String", "DEFAULT_SERVER_URL", "\"${setting("conversateUrl", "CONVERSATE_URL")}\"")
        buildConfigField("String", "DEFAULT_TOKEN", "\"${setting("conversateToken", "CONVERSATE_TOKEN")}\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    buildFeatures {
        buildConfig = true
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
