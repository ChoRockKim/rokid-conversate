buildscript {
    dependencies {
        // AGP 9 has built-in Kotlin support; pin the Kotlin Gradle plugin version it uses.
        classpath(libs.kotlin.gradle.plugin)
    }
}

plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.paparazzi) apply false
}
