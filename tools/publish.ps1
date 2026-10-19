# tools/publish.ps1 - run by publish.bat / release.bat (keep ASCII-only: Windows PowerShell 5.1 misreads UTF-8 without BOM)
#
#   publish.bat                 commit + push this repo (TraderOS)
#   publish.bat release         ... and tag the next version (v0.2.1 -> v0.2.2); GitHub Actions builds the Release
#   publish.bat release 0.3.0   ... tag this exact version
#   publish.bat -m "message"    commit message
#
# Commit message (no need to type one):
#   1) -m "..." if given
#   2) else publish.msg in the repo folder (UTF-8; first line = title, rest = details).
#      Claude writes this file when it changes files; it is deleted after a successful push.
#   3) else made from the changed paths, e.g. "update: web_ui, services/guidelines (3 files)"
# Release tag message: commit titles since the previous tag (made automatically).
# Release page text: .github/workflows/release.yml writes it from the full commit messages since the previous tag.
#
# Safety: stops before committing if data/, secrets/, uploads/, logs/, venv/ or a .env file would be committed,
# or if a changed file looks like it contains a secret (tools/check_secrets.py).

param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)
$ErrorActionPreference = 'Stop'

$release = $false; $version = $null; $msg = $null
for ($i = 0; $i -lt $Rest.Count; $i++) {
    $a = $Rest[$i]
    if ($a -eq 'release') { $release = $true }
    elseif ($a -eq '-m' -and $i + 1 -lt $Rest.Count) { $msg = $Rest[$i + 1]; $i++ }
    elseif ($a -match '^v?\d+\.\d+\.\d+$') { $version = $a.TrimStart('v') }
    else { Write-Host "  unknown argument: $a"; exit 1 }
}

$repo = Split-Path -Parent $PSScriptRoot
$msgFile = $null
if (-not $msg) {
    $f = Join-Path $repo 'publish.msg'
    if (Test-Path $f) {
        $t = (Get-Content -Raw -Encoding UTF8 $f)
        if ($t -and $t.Trim()) { $msg = $t.Trim(); $msgFile = $f }
    }
}
$utf8 = New-Object System.Text.UTF8Encoding $false
$tmpDir = if ($env:TEMP) { $env:TEMP } else { [IO.Path]::GetTempPath() }
$tmpMsg = Join-Path $tmpDir 'traderos-publish-msg.txt'

# the real git program (calling plain "git" inside a function named Git would call the function itself)
$gitExe = (Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $gitExe) { Write-Host "  git not found - install Git for Windows"; exit 1 }

function Step($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Git {
    # git writes progress/warnings (e.g. "LF will be replaced by CRLF") to stderr - only the exit code means failure
    $ErrorActionPreference = 'Continue'
    $out = & $gitExe -C $repo @args 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { $out | ForEach-Object { Write-Host "   $_" }; throw "git $($args -join ' ') failed" }
    return $out | Where-Object { $_ -notmatch '^warning: ' }
}
function FindPython {
    # full path of the program (a bare name could resolve to a PowerShell function of the same name)
    $venv = Join-Path $repo 'venv\Scripts\python.exe'
    if (Test-Path $venv) { return @($venv) }
    foreach ($n in @('py', 'python', 'python3')) {
        $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { if ($n -eq 'py') { return @($c.Source, '-3') } else { return @($c.Source) } }
    }
    throw "Python not found (needed for the secret check)"
}
function AutoMessage($files) {
    $parts = @($files | ForEach-Object {
        $f = $_ -replace '\\', '/'
        if ($f -match '^(web_ui|admin_ui|mt5_ea|alembic|tools|util|\.github)/') { $Matches[1] }
        elseif ($f -match '^(services|api|db|schemas|mt5)/(.+)$') { "$($Matches[1])/$([IO.Path]::GetFileNameWithoutExtension($Matches[2]))" }
        else { $f }
    } | Select-Object -Unique)
    $list = if ($parts.Count -gt 6) { ($parts[0..5] -join ', ') + ', ...' } else { $parts -join ', ' }
    return "update: $list ($(@($files).Count) files)"
}

try {
    Step "TraderOS : commit + push"
    Git add -A | Out-Null
    $staged = @(Git diff --cached --name-only --diff-filter=ACMR)
    $all = @(Git diff --cached --name-only)
    $bad = $all | Where-Object {
        ($_ -match '^(data|secrets|uploads|logs|venv|\.venv)/') -or (($_ -match '(^|/)\.env') -and ($_ -notmatch '\.env\.example$'))
    }
    if ($bad) {
        Git reset -q | Out-Null
        Write-Host "   STOP - these must not be committed:" -ForegroundColor Red
        $bad | ForEach-Object { Write-Host "     $_" -ForegroundColor Red }
        Write-Host "   Add them to .gitignore (if already tracked: git rm --cached <file>) and run again."
        exit 2
    }
    if ($staged.Count) {
        $py = @(FindPython)
        $paths = $staged | ForEach-Object { Join-Path $repo $_ }
        $pyExe = $py[0]
        $pyArgs = @($py | Select-Object -Skip 1) + @((Join-Path (Join-Path $repo 'tools') 'check_secrets.py')) + @($paths)
        & $pyExe @pyArgs
        if ($LASTEXITCODE -ne 0) { Git reset -q | Out-Null; exit 2 }
    }
    if (-not $all.Count) { Write-Host "   nothing to commit" }
    else {
        $m = if ($msg) { $msg } else { AutoMessage $all }
        Write-Host "   $($all.Count) file(s) - $(($m -split "`n")[0])"
        [IO.File]::WriteAllText($tmpMsg, $m + "`n", $utf8)     # file + UTF-8: Korean text stays intact
        Git commit -q -F $tmpMsg | Out-Null
    }
    Git push -q | Out-Null
    Write-Host "   pushed" -ForegroundColor Green
    if ($msgFile) { Remove-Item $msgFile -ErrorAction SilentlyContinue }   # used - next changes start a new one

    if ($release) {
        Step "release"
        Git fetch -q --tags | Out-Null
        $tags = @(Git tag --list 'v*' | ForEach-Object {
            $v = $_.ToString().TrimStart('v'); try { [version]$v } catch { $null } } | Where-Object { $_ } | Sort-Object)
        $last = if ($tags.Count) { $tags[-1] } else { $null }
        if (-not $version) {
            $version = if ($last) { "{0}.{1}.{2}" -f $last.Major, $last.Minor, ([Math]::Max($last.Build, 0) + 1) } else { '0.1.0' }
        }
        $tag = "v$version"
        if (Git tag --list $tag) { throw "tag $tag already exists - give a version: release.bat 0.3.0" }
        if ($last) { Write-Host "   latest: v$last" }
        $ans = Read-Host "   Create release $tag ? (y/N)"
        if ($ans -notmatch '^[yY]') { Write-Host "   skipped"; exit 0 }
        $range = if ($last) { "v$last..HEAD" } else { 'HEAD' }
        $log = Git log --format='- %s' -n 50 $range
        $notes = "TraderOS $tag`n`n" + (($log | ForEach-Object { "$_" }) -join "`n")
        [IO.File]::WriteAllText($tmpMsg, $notes + "`n", $utf8)
        Git tag -a $tag -F $tmpMsg | Out-Null
        Git push -q origin $tag | Out-Null
        $url = (Git remote get-url origin | Select-Object -First 1).ToString() -replace '\.git$', '' -replace '^git@github\.com:', 'https://github.com/'
        Write-Host "   tagged $tag - GitHub is building the release (1-2 min):" -ForegroundColor Green
        Write-Host "   $url/releases"
    }
    Write-Host ""
    Write-Host "Done." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
