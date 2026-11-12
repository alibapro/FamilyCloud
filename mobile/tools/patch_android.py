"""Настраивает Android-проект, созданный `flutter create`, под FamilyCloud.

    python tools/patch_android.py <папка_проекта>

Что делает (повторный запуск безопасен):
  * разрешения в AndroidManifest.xml, название приложения
  * MainActivity -> FlutterFragmentActivity (нужно для отпечатка пальца)
  * minSdk 24, core library desugaring (нужно для уведомлений)
  * подпись release-сборки ключом из android/key.properties (если он есть)
"""
import re
import sys
from pathlib import Path

PERMISSIONS = [
    "android.permission.INTERNET",
    "android.permission.READ_MEDIA_IMAGES",
    "android.permission.READ_MEDIA_VIDEO",
    "android.permission.READ_MEDIA_VISUAL_USER_SELECTED",
    "android.permission.ACCESS_MEDIA_LOCATION",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.USE_BIOMETRIC",
    "android.permission.RECEIVE_BOOT_COMPLETED",
]
DESUGAR = "com.android.tools:desugar_jdk_libs:2.1.4"


def patch_manifest(p: Path) -> None:
    s = p.read_text(encoding="utf-8")
    lines = []
    for perm in PERMISSIONS:
        if perm not in s:
            lines.append(f'    <uses-permission android:name="{perm}"/>')
    if "READ_EXTERNAL_STORAGE" not in s:
        lines.append('    <uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" android:maxSdkVersion="32"/>')
    if lines:
        s = re.sub(r"(<manifest[^>]*>)", lambda m: m.group(1) + "\n" + "\n".join(lines), s, count=1)
    s = re.sub(r'android:label="[^"]*"', 'android:label="FamilyCloud"', s, count=1)
    p.write_text(s, encoding="utf-8")
    print("  манифест: OK")


def patch_activity(android: Path) -> None:
    for p in list(android.rglob("MainActivity.kt")) + list(android.rglob("MainActivity.java")):
        s = p.read_text(encoding="utf-8")
        s = s.replace("io.flutter.embedding.android.FlutterActivity", "io.flutter.embedding.android.FlutterFragmentActivity")
        s = re.sub(r"\bFlutterActivity\b", "FlutterFragmentActivity", s)
        p.write_text(s, encoding="utf-8")
        print(f"  {p.name}: OK")


def patch_gradle_kts(p: Path) -> None:
    s = p.read_text(encoding="utf-8")
    if "isCoreLibraryDesugaringEnabled" not in s:
        s = s.replace("compileOptions {", "compileOptions {\n        isCoreLibraryDesugaringEnabled = true", 1)
    s = re.sub(r"minSdk\s*=\s*flutter\.minSdkVersion", "minSdk = 24", s)
    if "java.util.Properties" not in s:
        s = (
            "import java.util.Properties\nimport java.io.FileInputStream\n\n" + s
        )
    if "keystoreProperties" not in s:
        s = s.replace(
            "android {",
            'val keystoreProperties = Properties()\n'
            'val keystorePropertiesFile = rootProject.file("key.properties")\n'
            "if (keystorePropertiesFile.exists()) {\n"
            "    keystoreProperties.load(FileInputStream(keystorePropertiesFile))\n"
            "}\n\nandroid {",
            1,
        )
        s = s.replace(
            "    buildTypes {",
            "    signingConfigs {\n"
            '        create("release") {\n'
            "            if (keystorePropertiesFile.exists()) {\n"
            '                keyAlias = keystoreProperties["keyAlias"] as String\n'
            '                keyPassword = keystoreProperties["keyPassword"] as String\n'
            '                storeFile = file(keystoreProperties["storeFile"] as String)\n'
            '                storePassword = keystoreProperties["storePassword"] as String\n'
            "            }\n"
            "        }\n"
            "    }\n\n"
            "    buildTypes {",
            1,
        )
        s = s.replace(
            'signingConfig = signingConfigs.getByName("debug")',
            'signingConfig = if (keystorePropertiesFile.exists()) signingConfigs.getByName("release") else signingConfigs.getByName("debug")',
        )
    if DESUGAR not in s:
        s += f'\ndependencies {{\n    coreLibraryDesugaring("{DESUGAR}")\n}}\n'
    p.write_text(s, encoding="utf-8")
    print("  build.gradle.kts: OK")


def patch_gradle_groovy(p: Path) -> None:
    s = p.read_text(encoding="utf-8")
    if "coreLibraryDesugaringEnabled" not in s:
        s = s.replace("compileOptions {", "compileOptions {\n        coreLibraryDesugaringEnabled true", 1)
    s = re.sub(r"minSdk(Version)?\s*=?\s*flutter\.minSdkVersion", "minSdkVersion 24", s)
    if "keystoreProperties" not in s:
        s = s.replace(
            "android {",
            "def keystoreProperties = new Properties()\n"
            "def keystorePropertiesFile = rootProject.file('key.properties')\n"
            "if (keystorePropertiesFile.exists()) {\n"
            "    keystoreProperties.load(new FileInputStream(keystorePropertiesFile))\n"
            "}\n\nandroid {",
            1,
        )
        s = s.replace(
            "    buildTypes {",
            "    signingConfigs {\n"
            "        release {\n"
            "            if (keystorePropertiesFile.exists()) {\n"
            "                keyAlias keystoreProperties['keyAlias']\n"
            "                keyPassword keystoreProperties['keyPassword']\n"
            "                storeFile file(keystoreProperties['storeFile'])\n"
            "                storePassword keystoreProperties['storePassword']\n"
            "            }\n"
            "        }\n"
            "    }\n\n"
            "    buildTypes {",
            1,
        )
        s = s.replace(
            "signingConfig = signingConfigs.debug",
            "signingConfig = keystorePropertiesFile.exists() ? signingConfigs.release : signingConfigs.debug",
        ).replace(
            "signingConfig signingConfigs.debug",
            "signingConfig keystorePropertiesFile.exists() ? signingConfigs.release : signingConfigs.debug",
        )
    if DESUGAR not in s:
        s += f"\ndependencies {{\n    coreLibraryDesugaring '{DESUGAR}'\n}}\n"
    p.write_text(s, encoding="utf-8")
    print("  build.gradle: OK")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = Path(sys.argv[1])
    android = root / "android"
    app = android / "app"
    if not app.exists():
        sys.exit(f"Не найден {app} — сначала выполните flutter create")
    print("Настраиваю Android-проект:")
    patch_manifest(app / "src" / "main" / "AndroidManifest.xml")
    patch_activity(app / "src" / "main")
    if (app / "build.gradle.kts").exists():
        patch_gradle_kts(app / "build.gradle.kts")
    elif (app / "build.gradle").exists():
        patch_gradle_groovy(app / "build.gradle")
    else:
        sys.exit("Не найден android/app/build.gradle(.kts)")
    print("Готово.")


if __name__ == "__main__":
    main()
