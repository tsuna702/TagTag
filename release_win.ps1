# Windows 판 빌드 → zip → GitHub 릴리스에 올리기.
# 버전은 packaging/LocalTag.spec 의 CFBundleShortVersionString 을 따른다 (macOS 와 같은 번호).
# 같은 버전의 릴리스가 있으면 Windows zip 을 붙이고 노트에 Windows 부분을 더한다. 없으면 새로 만든다.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Repo = "tsuna702/TagTag"

function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }
function Git { & git @args; if ($LASTEXITCODE) { Fail "git $($args -join ' ') 실패" } }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Fail "gh 가 없습니다. winget install GitHub.cli 후 gh auth login 을 해 주세요." }
& gh auth status *> $null
if ($LASTEXITCODE) { Fail "gh 로그인이 필요합니다. gh auth login 을 먼저 해 주세요." }

$branch = (git branch --show-current)
if ($branch -ne "windows") { Fail "Windows 릴리스는 windows 브랜치에서만 합니다 (지금: $branch)." }
git diff --quiet; $d1 = $LASTEXITCODE
git diff --cached --quiet; $d2 = $LASTEXITCODE
if ($d1 -or $d2) { Fail "커밋하지 않은 변경이 있습니다. 먼저 커밋해 주세요." }

$Version = (Select-String -Path packaging\LocalTag.spec -Pattern '"CFBundleShortVersionString":\s*"([^"]+)"').Matches[0].Groups[1].Value
$Tag = "v$Version"
$Zip = "dist\LocalTag-$Version-windows.zip"
Git fetch -q --tags origin

& gh release view $Tag --repo $Repo *> $null
$exists = ($LASTEXITCODE -eq 0)
if ($exists) {
    $assets = (gh release view $Tag --repo $Repo --json assets -q ".assets[].name") -split "`n"
    if ($assets -contains (Split-Path $Zip -Leaf)) {
        Fail "$Tag 릴리스에 이미 Windows zip 이 있습니다. packaging/LocalTag.spec 의 버전을 올려 주세요."
    }
}

& .\build_win.ps1
if ($LASTEXITCODE) { Fail "빌드 실패" }

$install = @(
    "### Windows 설치"
    "1. ``LocalTag-$Version-windows.zip``을 내려받아 압축을 풉니다."
    "2. ``LocalTag`` 폴더 안의 ``LocalTag.exe``를 실행합니다."
    "3. 서명하지 않은 앱이라 처음에 'Windows의 PC 보호'가 뜨면 **추가 정보 → 실행**을 누릅니다."
    ""
    "Windows 판에는 Apple Music 연동(Music 메뉴, 내 보관함 탭)이 없습니다."
)
$notes = New-TemporaryFile
if ($exists) {
    # macOS 릴리스 노트는 그대로 두고, 그 태그 이후 windows 브랜치에만 있는 커밋과 설치 안내를 더한다
    $body = gh release view $Tag --repo $Repo --json body -q ".body"
    $winLog = git log "$Tag..HEAD" --no-merges --pretty="- %s"
    $lines = @($body, "", "### Windows 판")
    if ($winLog) { $lines += $winLog }
    $lines += @("") + $install
} else {
    $prev = git tag --sort=-v:refname | Where-Object { $_ -like "v*" } | Select-Object -First 1
    $log = if ($prev) { git log "$prev..HEAD" --no-merges --pretty="- %s" } else { git log --no-merges --pretty="- %s" }
    $lines = @("## LocalTag $Version", "", "### 바뀐 점") + $log + @("") + $install
}
[IO.File]::WriteAllLines($notes, [string[]]$lines, (New-Object Text.UTF8Encoding $false))

Git push -q origin windows
if ($exists) {
    gh release upload $Tag $Zip --repo $Repo
    if ($LASTEXITCODE) { Fail "zip 올리기 실패" }
    gh release edit $Tag --repo $Repo --notes-file $notes
} else {
    gh release create $Tag $Zip --repo $Repo --title "LocalTag $Version" --notes-file $notes --target windows
}
if ($LASTEXITCODE) { Fail "릴리스 실패" }
Remove-Item $notes
Write-Host "릴리스 완료: https://github.com/$Repo/releases/tag/$Tag"
