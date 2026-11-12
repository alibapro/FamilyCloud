# Сборка FamilyCloud.apk на Windows.
# Требуется: Flutter (flutter doctor без ошибок для Android) и Python.
# Запуск:  powershell -ExecutionPolicy Bypass -File build_android.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$app  = Join-Path $here "build\familycloud_app"

if (-not (Get-Command flutter -ErrorAction SilentlyContinue)) {
    throw "Flutter не найден. Установите: https://docs.flutter.dev/get-started/install/windows/mobile"
}

# 1. Проект Flutter (создаётся один раз)
if (-not (Test-Path $app)) {
    Write-Host "Создаю проект Flutter..." -ForegroundColor Cyan
    flutter create --org kz.familycloud --project-name familycloud --platforms android $app
    if ($LASTEXITCODE -ne 0) { throw "flutter create завершился с ошибкой" }
}

# 2. Наш код поверх шаблона
Copy-Item (Join-Path $here "pubspec.yaml") $app -Force
Remove-Item (Join-Path $app "lib")  -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $app "test") -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $here "lib") (Join-Path $app "lib") -Recurse

# 3. Настройки Android (разрешения, minSdk, подпись)
python (Join-Path $here "tools\patch_android.py") $app
if ($LASTEXITCODE -ne 0) { throw "patch_android.py завершился с ошибкой" }

# 4. Ключ подписи (создаётся один раз — ХРАНИТЕ его: без него не установить обновление поверх)
$ks     = Join-Path $here "familycloud-release.jks"
$pwFile = Join-Path $here "keystore-password.txt"
if (-not (Test-Path $ks)) {
    $keytool = (Get-Command keytool -ErrorAction SilentlyContinue).Source
    if (-not $keytool) {
        $cand = @(
            "$env:ProgramFiles\Android\Android Studio\jbr\bin\keytool.exe",
            "$env:ProgramFiles\Android\Android Studio\jre\bin\keytool.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        $keytool = $cand
    }
    if (-not $keytool) { throw "keytool не найден (входит в Android Studio)" }
    $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $pw = -join (1..24 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
    Write-Host "Создаю ключ подписи..." -ForegroundColor Cyan
    & $keytool -genkeypair -keystore $ks -alias familycloud -keyalg RSA -keysize 4096 -validity 10000 `
        -storepass $pw -keypass $pw -dname "CN=FamilyCloud, C=KZ"
    if ($LASTEXITCODE -ne 0) { throw "keytool завершился с ошибкой" }
    Set-Content -Path $pwFile -Value $pw -Encoding ascii
}
$pw = (Get-Content $pwFile -Raw).Trim()
$ksPath = $ks.Replace('\', '/')
@"
storePassword=$pw
keyPassword=$pw
keyAlias=familycloud
storeFile=$ksPath
"@ | Set-Content -Path (Join-Path $app "android\key.properties") -Encoding ascii

# 5. Сборка
Push-Location $app
try {
    flutter pub get
    if ($LASTEXITCODE -ne 0) { throw "flutter pub get: ошибка зависимостей" }
    flutter build apk --release
    if ($LASTEXITCODE -ne 0) { throw "Сборка не удалась — пришлите текст ошибки" }
} finally { Pop-Location }

$out = Join-Path $here "FamilyCloud.apk"
Copy-Item (Join-Path $app "build\app\outputs\flutter-apk\app-release.apk") $out -Force
Write-Host "`nГотово: $out" -ForegroundColor Green
Write-Host "Скопируйте APK на телефон и установите (разрешите установку из этого источника)."
