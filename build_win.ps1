# LocalTag Windows 빌드 → dist\LocalTag\LocalTag.exe, dist\LocalTag-<버전>-windows.zip
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv\Scripts\python.exe)) {
    py -3 -m venv .venv
    if ($LASTEXITCODE) { throw "venv 를 만들지 못했습니다 (Python 3 설치 확인)" }
}
.venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller
if ($LASTEXITCODE) { throw "패키지 설치 실패" }
if (-not (Test-Path packaging\LocalTag.ico)) { .venv\Scripts\python packaging\make_icon.py }

.venv\Scripts\pyinstaller --noconfirm --clean --distpath dist --workpath build packaging\LocalTag-win.spec
if ($LASTEXITCODE) { throw "pyinstaller 실패" }

$version = (Select-String -Path packaging\LocalTag.spec -Pattern '"CFBundleShortVersionString":\s*"([^"]+)"').Matches[0].Groups[1].Value
$zip = "dist\LocalTag-$version-windows.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\LocalTag -DestinationPath $zip
Write-Host ""
Write-Host "완료: $(Resolve-Path dist\LocalTag\LocalTag.exe)"
Write-Host "      $(Resolve-Path $zip)"
