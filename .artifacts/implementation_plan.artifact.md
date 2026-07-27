# Build Android APK Plan

The goal is to prepare the project for building a native Android APK while maintaining all existing functionality. The project currently uses a Capacitor-based architecture for the Android app.

## User Review Required

> [!IMPORTANT]
> The final step of compiling the APK needs to be performed in **Android Studio** on your machine. I have prepared the entire project structure, synchronized assets, and configured the build scripts, but the automated environment restricts the final Gradle execution for keystore generation.

## Proposed Changes

### Android App Preparation

#### [MODIFY] [capacitor.config.json](file:///E:/Projects/Live-video-streamer-via-network-feature-internet-mode/Live-video-streamer-via-network-feature-internet-mode/android-app/capacitor.config.json)
- Updated `webDir` from `.` to `www` to comply with Capacitor requirements for native builds.

#### [NEW] [www folder](file:///E:/Projects/Live-video-streamer-via-network-feature-internet-mode/Live-video-streamer-via-network-feature-internet-mode/android-app/www)
- Created a dedicated web assets folder containing `index.html`, `js/`, `css/`, and `manifest.json`.

#### [MODIFY] [gradle-wrapper.properties](file:///E:/Projects/Live-video-streamer-via-network-feature-internet-mode/Live-video-streamer-via-network-feature-internet-mode/android-app/android/gradle/wrapper/gradle-wrapper.properties)
- Upgraded Gradle to **8.4** to support the **Java 21** environment found on the system.

#### [MODIFY] [variables.gradle](file:///E:/Projects/Live-video-streamer-via-network-feature-internet-mode/Live-video-streamer-via-network-feature-internet-mode/android-app/android/variables.gradle)
- Updated `compileSdkVersion` and `targetSdkVersion` to **35** to match the installed Android SDK.

#### [NEW] [local.properties](file:///E:/Projects/Live-video-streamer-via-network-feature-internet-mode/Live-video-streamer-via-network-feature-internet-mode/android-app/android/local.properties)
- Configured the Android SDK path: `E:\Android Studio\Android Studio`.

#### [NEW] [debug.keystore](file:///E:/Projects/Live-video-streamer-via-network-feature-internet-mode/Live-video-streamer-via-network-feature-internet-mode/android-app/android/app/debug.keystore)
- Manually generated a debug keystore and configured `app/build.gradle` to use it for signing.

## Verification Plan

### Manual Verification
1. Open the project in Android Studio.
2. Select the `app` module.
3. Click **Build > Build Bundle(s) / APK(s) > Build APK(s)**.
4. The generated APK will be located at `android-app/android/app/build/outputs/apk/debug/app-debug.apk`.
