[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
if (-not $repoRoot) { $repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
$port = 8766
if ($env:CODEX_SHIM_PORT) { $port = [int]$env:CODEX_SHIM_PORT }
$templatePath = Join-Path $HOME ".codex-shim\models.json"
$resolvedPath = Join-Path $HOME ".codex-shim\models.resolved.json"
$catalogPath = Join-Path $repoRoot ".codex-shim\custom_model_catalog.json"

$costInfo = @{
    "glm-5.1"           = @{ cost = 0.014; tier = "Premium";   limit5h = 880;   limitMo = 4300 }
    "glm-5"             = @{ cost = 0.010; tier = "Premium";   limit5h = 1150;  limitMo = 5750 }
    "kimi-k2.5"         = @{ cost = 0.006; tier = "Standard";  limit5h = 1850;  limitMo = 9250 }
    "kimi-k2.6"         = @{ cost = 0.010; tier = "Premium";   limit5h = 1150;  limitMo = 5750 }
    "deepseek-v4-pro"   = @{ cost = 0.003; tier = "Standard";  limit5h = 3450;  limitMo = 17150 }
    "deepseek-v4-flash" = @{ cost = 0.0004; tier = "Economy";  limit5h = 31650; limitMo = 158150 }
    "mimo-v2.5"         = @{ cost = 0.0004; tier = "Economy";  limit5h = 30100; limitMo = 150400 }
    "mimo-v2.5-pro"     = @{ cost = 0.004; tier = "Standard";  limit5h = 3250;  limitMo = 16300 }
    "minimax-m2.7"      = @{ cost = 0.004; tier = "Standard";  limit5h = 3400;  limitMo = 17000 }
    "minimax-m2.5"      = @{ cost = 0.002; tier = "Standard";  limit5h = 6300;  limitMo = 31800 }
    "qwen3.7-max"       = @{ cost = 0.012; tier = "Premium";   limit5h = 950;   limitMo = 4770 }
    "qwen3.6-plus"      = @{ cost = 0.004; tier = "Standard";  limit5h = 3300;  limitMo = 16300 }
}
$tierColor = @{ "Economy" = "Green"; "Standard" = "Yellow"; "Premium" = "Red" }

function Get-ShimCommand {
    $py = Get-Command "py" -ErrorAction SilentlyContinue
    if ($py) { return [PSCustomObject]@{ File = $py.Source; Args = @("-3", "-m", "codex_shim.cli") } }
    $python3 = Get-Command "python3" -ErrorAction SilentlyContinue
    if ($python3) { return [PSCustomObject]@{ File = $python3.Source; Args = @("-m", "codex_shim.cli") } }
    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if ($python) { return [PSCustomObject]@{ File = $python.Source; Args = @("-m", "codex_shim.cli") } }
    $installed = Get-Command "codex-shim" -ErrorAction SilentlyContinue
    if ($installed) { return [PSCustomObject]@{ File = $installed.Source; Args = @() } }
    throw "Could not find Python or codex-shim on PATH. Install with: py -3.11 -m pip install --user -e ."
}

$shimCommand = Get-ShimCommand

function Invoke-Shim {
    param([string[]]$Arguments)

    $oldPythonPath = $env:PYTHONPATH
    try {
        if ($oldPythonPath) { $env:PYTHONPATH = "$repoRoot;$oldPythonPath" } else { $env:PYTHONPATH = $repoRoot }
        $baseArgs = @($shimCommand.Args) + @("--settings", $resolvedPath, "--port", [string]$port) + @($Arguments)
        & $shimCommand.File @baseArgs
    } finally {
        if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $oldPythonPath }
    }
}

function Get-EnvVarValue($name) {
    foreach ($scope in @("Process", "User", "Machine")) {
        $value = [Environment]::GetEnvironmentVariable($name, $scope)
        if ($value) { return $value }
    }
    return $null
}

function Set-EnvVarForChild($name, $value) {
    Set-Item -Path "Env:$name" -Value $value
}

function Get-PlaceholderNames($text) {
    $names = @{}
    foreach ($match in [regex]::Matches($text, '%%([A-Za-z_][A-Za-z0-9_]*)%%')) { $names[$match.Groups[1].Value] = $true }
    foreach ($match in [regex]::Matches($text, '\$\{([A-Za-z_][A-Za-z0-9_]*)\}')) { $names[$match.Groups[1].Value] = $true }
    return @($names.Keys)
}

function Get-ZaiQuota {
    $key = Get-EnvVarValue "ZAI_API_KEY"
    if (-not $key) { return $null }
    try {
        return Invoke-RestMethod -Uri "https://api.z.ai/api/monitor/usage/quota/limit" -Headers @{
            Authorization = $key
            "Accept-Language" = "en-US,en"
            "Content-Type" = "application/json"
        } -TimeoutSec 8
    } catch { return $null }
}

function Get-CodexQuota {
    $authPath = Join-Path $HOME ".codex\auth.json"
    if (-not (Test-Path $authPath)) { return $null }
    try {
        $auth = Get-Content $authPath -Raw | ConvertFrom-Json
        $token = $auth.tokens.access_token
        $acctId = $auth.tokens.account_id
        if (-not $token -or -not $acctId) { return $null }
        return Invoke-RestMethod -Uri "https://chatgpt.com/backend-api/wham/usage" -Headers @{
            Authorization = "Bearer $token"
            Accept = "application/json"
            "ChatGPT-Account-Id" = $acctId
            Origin = "https://chatgpt.com"
            Referer = "https://chatgpt.com/"
            "User-Agent" = "Mozilla/5.0"
        } -TimeoutSec 8
    } catch { return $null }
}

function Format-ResetTime($ts) {
    if ($null -eq $ts) { return "unknown" }
    $dt = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$ts).LocalDateTime
    $diff = $dt - (Get-Date)
    if ($diff.TotalSeconds -le 0) { return "now" }
    if ($diff.TotalHours -gt 24) { return "{0}d {1}h" -f [math]::Floor($diff.TotalDays), $diff.Hours }
    return "{0}h {1}m" -f [math]::Floor($diff.TotalHours), $diff.Minutes
}

function Format-PercentBar($pct) {
    if ($null -eq $pct) { $pct = 0 }
    $pct = [math]::Min(100, [math]::Max(0, [double]$pct))
    $filled = [int][math]::Floor($pct / 10)
    $empty = 10 - $filled
    $bar = ("#" * $filled) + ("-" * $empty)
    if ($pct -ge 90) { $color = "Red" }
    elseif ($pct -ge 60) { $color = "Yellow" }
    else { $color = "Green" }
    return @{ bar = $bar; color = $color }
}

function Format-TitleCase($value) {
    if (-not $value) { return "" }
    $text = [string]$value
    return $text.Substring(0, 1).ToUpper() + $text.Substring(1).ToLower()
}

function Get-CodexDisplayName {
    $codexQ = Get-CodexQuota
    if (-not $codexQ -or -not $codexQ.rate_limit) { return "GPT-5.5" }
    $plan = ([string]$codexQ.plan_type).ToUpper()
    $pri = $codexQ.rate_limit.primary_window
    $sec = $codexQ.rate_limit.secondary_window
    $priReset = ""
    if ($pri.reset_after_seconds) {
        $h = [math]::Floor($pri.reset_after_seconds / 3600)
        $m = [math]::Floor(($pri.reset_after_seconds % 3600) / 60)
        $priReset = " ${h}h${m}m"
    }
    return "GPT-5.5 ($plan | 5h:$($pri.used_percent)%$priReset wk:$($sec.used_percent)%)"
}

function Get-ModelRows($json) {
    if ($json -is [array]) { return @($json) }
    foreach ($key in @("models", "customModels", "launchModels", "launch_models")) {
        if ($json.PSObject.Properties.Name -contains $key) { return @($json.$key) }
    }
    return @()
}

function Get-ModelId($model) {
    if ($model.PSObject.Properties.Name -contains "model") { return [string]$model.model }
    return ""
}

function Get-ModelDisplayName($model) {
    foreach ($key in @("display_name", "displayName", "name")) {
        if ($model.PSObject.Properties.Name -contains $key) { return [string]$model.$key }
    }
    return (Get-ModelId $model)
}

function Set-ModelDisplayName($model, $value) {
    foreach ($key in @("display_name", "displayName", "name")) {
        if ($model.PSObject.Properties.Name -contains $key) { $model.$key = $value; return }
    }
    $model | Add-Member -NotePropertyName "display_name" -NotePropertyValue $value -Force
}

function Resolve-Keys {
    if (Test-Path $templatePath) {
        $raw = [System.IO.File]::ReadAllText($templatePath)
    } else {
        Write-Host "  No $templatePath found; using ChatGPT/Codex passthrough only." -ForegroundColor Yellow
        $raw = '{"models":[]}'
    }

    $placeholderNames = Get-PlaceholderNames $raw
    $missing = @()
    foreach ($varName in $placeholderNames) {
        $val = Get-EnvVarValue $varName
        if ($val) { Set-EnvVarForChild $varName $val } else { $missing += $varName }
    }
    foreach ($varName in $missing) {
        Write-Host "  Missing env var: $varName" -ForegroundColor Red
        Write-Host "    setx $varName `"your-key`"" -ForegroundColor White
        Write-Host "  Enter key for $varName (or Enter to skip): " -NoNewline -ForegroundColor Green
        $inputKey = Read-Host
        if ($inputKey) {
            Set-EnvVarForChild $varName $inputKey
            setx $varName $inputKey | Out-Null
            Write-Host "  Saved $varName for future shells." -ForegroundColor Green
        } else {
            Write-Host "  Continuing without $varName; models using it may be unavailable." -ForegroundColor Yellow
        }
    }

    $raw = [regex]::Replace($raw, '%%([A-Za-z_][A-Za-z0-9_]*)%%', { param($match) '${' + $match.Groups[1].Value + '}' })
    try {
        $json = $raw | ConvertFrom-Json
    } catch {
        Write-Host "  $templatePath is not valid JSON: $_" -ForegroundColor Red
        return $false
    }

    $zaiResp = Get-ZaiQuota
    $zaiQ = $null
    if ($zaiResp -and $zaiResp.success) { $zaiQ = $zaiResp.data }
    $zaiLevel = $null
    $zaiTag = ""
    if ($zaiQ) {
        $zaiLevel = Format-TitleCase $zaiQ.level
        $tok5h = ($zaiQ.limits | Where-Object { $_.type -eq "TOKENS_LIMIT" -and $_.unit -eq 3 } | Select-Object -First 1).percentage
        $week  = ($zaiQ.limits | Where-Object { $_.type -eq "TOKENS_LIMIT" -and $_.unit -eq 6 } | Select-Object -First 1).percentage
        $zaiTag = " ($zaiLevel | 5h:${tok5h}% wk:${week}%)"
    }

    foreach ($m in Get-ModelRows $json) {
        $modelId = Get-ModelId $m
        $displayName = Get-ModelDisplayName $m
        if ($zaiQ -and ($modelId -eq "glm-5.1" -or $displayName -eq "Z.AI GLM 5.1")) {
            Set-ModelDisplayName $m "Z.AI $zaiLevel | GLM 5.1$zaiTag"
        } elseif ($zaiQ -and ($modelId -eq "glm-5" -or $displayName -eq "Z.AI GLM 5")) {
            Set-ModelDisplayName $m "Z.AI $zaiLevel | GLM 5$zaiTag"
        }
        if ($displayName -match "^Go ") {
            $c = $costInfo[$modelId]
            if ($c) {
                $baseName = $displayName.Substring(3) -replace '\s+\|\s+(Economy|Standard|Premium)\s+~\$.*?/req$', ''
                Set-ModelDisplayName $m "Go $baseName | $($c.tier) ~`$$($c.cost)/req"
            }
        }
    }

    $resolvedDir = Split-Path -Parent $resolvedPath
    if (-not (Test-Path $resolvedDir)) { New-Item -ItemType Directory -Path $resolvedDir | Out-Null }
    $resolvedJson = ConvertTo-Json -InputObject $json -Depth 20
    [System.IO.File]::WriteAllText($resolvedPath, $resolvedJson + "`n", (New-Object System.Text.UTF8Encoding $false))
    return $true
}

function Patch-Catalog {
    if (-not (Test-Path $catalogPath)) { return }
    $newName = Get-CodexDisplayName
    if ($newName -eq "GPT-5.5") { return }
    try {
        $catalog = Get-Content $catalogPath -Raw | ConvertFrom-Json
    } catch { return }

    $changed = $false
    foreach ($m in @($catalog.models)) {
        if ($m.slug -eq "gpt-5.5") {
            $m.display_name = $newName
            if ($m.model_messages -and $m.model_messages.instructions_variables) {
                $m.model_messages.instructions_variables.model_name = $newName
            }
            $changed = $true
        }
    }
    if ($changed) {
        $raw = ConvertTo-Json -InputObject $catalog -Depth 20
        [System.IO.File]::WriteAllText($catalogPath, $raw + "`n", (New-Object System.Text.UTF8Encoding $false))
    }
}

function Ensure-ShimRunning {
    $status = Invoke-Shim @("status") 2>&1
    if ($LASTEXITCODE -eq 0 -and ($status -match "running")) { return }
    Write-Host "`n  Starting shim on port $port..." -ForegroundColor Yellow
    Invoke-Shim @("start") 2>&1 | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) { throw "codex-shim failed to start." }
    Start-Sleep -Milliseconds 500
}

function Get-Models {
    $output = Invoke-Shim @("model", "list") 2>&1
    $models = @()
    foreach ($line in $output) {
        if ($line -match '^(\S+)\s{2,}(.+)\s+->\s+(.+)\s+\(([^()]*)\)$') {
            $models += [PSCustomObject]@{
                Slug     = $Matches[1].Trim()
                Name     = $Matches[2].Trim()
                UpModel  = $Matches[3].Trim()
                Provider = $Matches[4].Trim()
            }
        }
    }
    return $models
}

function Set-LoopbackNoProxy {
    foreach ($key in @("NO_PROXY", "no_proxy")) {
        $values = @()
        if ((Get-Item "Env:$key" -ErrorAction SilentlyContinue) -and (Get-Item "Env:$key").Value) {
            $values = ((Get-Item "Env:$key").Value -split ',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        }
        foreach ($hostName in @("127.0.0.1", "localhost", "::1")) {
            if (($values | ForEach-Object { $_.ToLowerInvariant() }) -notcontains $hostName.ToLowerInvariant()) { $values += $hostName }
        }
        Set-Item -Path "Env:$key" -Value ($values -join ',')
    }
}

function Launch-CodexApp {
    $codex = Get-Command "codex" -ErrorAction SilentlyContinue
    if (-not $codex) { throw "codex command not found on PATH. Install/authenticate Codex CLI first." }
    Set-LoopbackNoProxy
    & codex app .
}

function Show-Menu {
    Clear-Host
    Write-Host ""
    Write-Host "  ========================================" -ForegroundColor Cyan
    Write-Host "       CODEX-SHIM  MODEL  LAUNCHER" -ForegroundColor Cyan
    Write-Host "  ========================================" -ForegroundColor Cyan
    Write-Host ""

    # --- Z.AI Live Quota ---
    $zaiResp = Get-ZaiQuota
    $zaiQ = $null
    if ($zaiResp -and $zaiResp.success) { $zaiQ = $zaiResp.data }
    if ($zaiQ) {
        $level = ([string]$zaiQ.level).ToUpper()
        Write-Host "  Z.AI CODING PLAN ($level)" -ForegroundColor Magenta
        foreach ($lim in $zaiQ.limits) {
            if ($lim.type -eq "TIME_LIMIT") {
                $pct = $lim.percentage
                $pb = Format-PercentBar $pct
                $reset = Format-ResetTime $lim.nextResetTime
                Write-Host "    MCP Monthly:  " -NoNewline
                Write-Host "[$($pb.bar)]" -NoNewline -ForegroundColor $pb.color
                Write-Host " $($lim.currentValue)/$($lim.number) calls (${pct}%)  resets in $reset" -ForegroundColor DarkGray
            }
            if ($lim.type -eq "TOKENS_LIMIT" -and $lim.unit -eq 3) {
                $pct = $lim.percentage
                $pb = Format-PercentBar $pct
                $reset = Format-ResetTime $lim.nextResetTime
                Write-Host "    5h Tokens:    " -NoNewline
                Write-Host "[$($pb.bar)]" -NoNewline -ForegroundColor $pb.color
                Write-Host " ${pct}% used  resets in $reset" -ForegroundColor DarkGray
            }
            if ($lim.type -eq "TOKENS_LIMIT" -and $lim.unit -eq 6) {
                $pct = $lim.percentage
                $pb = Format-PercentBar $pct
                $reset = Format-ResetTime $lim.nextResetTime
                Write-Host "    Weekly:       " -NoNewline
                Write-Host "[$($pb.bar)]" -NoNewline -ForegroundColor $pb.color
                Write-Host " ${pct}% used  resets in $reset" -ForegroundColor DarkGray
            }
        }
        Write-Host ""
    }

    # --- Codex/ChatGPT Live Quota ---
    $codexQ = Get-CodexQuota
    if ($codexQ -and $codexQ.rate_limit) {
        $plan = ([string]$codexQ.plan_type).ToUpper()
        $pri = $codexQ.rate_limit.primary_window
        $sec = $codexQ.rate_limit.secondary_window
        $limitReached = $codexQ.rate_limit.limit_reached
        Write-Host "  CODEX / CHATGPT ($plan)" -ForegroundColor DarkYellow
        if ($limitReached) {
            Write-Host "    STATUS: " -NoNewline; Write-Host "LIMIT REACHED" -ForegroundColor Red
        }
        $pb5h = Format-PercentBar $pri.used_percent
        $reset5h = ""
        if ($pri.reset_after_seconds) {
            $h = [math]::Floor($pri.reset_after_seconds / 3600)
            $m = [math]::Floor(($pri.reset_after_seconds % 3600) / 60)
            $reset5h = "  resets in ${h}h ${m}m"
        }
        Write-Host "    5h Window:   " -NoNewline
        Write-Host "[$($pb5h.bar)]" -NoNewline -ForegroundColor $pb5h.color
        Write-Host " $($pri.used_percent)% used$reset5h" -ForegroundColor DarkGray
        $pbWk = Format-PercentBar $sec.used_percent
        $resetWk = ""
        if ($sec.reset_after_seconds) {
            $d = [math]::Floor($sec.reset_after_seconds / 86400)
            $h = [math]::Floor(($sec.reset_after_seconds % 86400) / 3600)
            $resetWk = "  resets in ${d}d ${h}h"
        }
        Write-Host "    Weekly:      " -NoNewline
        Write-Host "[$($pbWk.bar)]" -NoNewline -ForegroundColor $pbWk.color
        Write-Host " $($sec.used_percent)% used$resetWk" -ForegroundColor DarkGray
        Write-Host ""
    }

    $models = Get-Models
    if ($models.Count -eq 0) {
        Write-Host "  No models found." -ForegroundColor Red
        Write-Host ""
        pause
        return $null
    }

    # Build display-ordered list (matches what user sees)
    $zai = @($models | Where-Object { $_.Name -match "^Z\.AI" })
    $go  = @($models | Where-Object { $_.Name -match "^Go " })
    $other = @($models | Where-Object { $_.Name -notmatch "^(Z\.AI|Go )" })
    $ordered = @($zai) + @($go) + @($other)

    $i = 1
    if ($zai.Count -gt 0) {
        Write-Host "  -- Z.AI Models --------------------------" -ForegroundColor Magenta
        foreach ($m in $zai) {
            Write-Host "    [$i] " -NoNewline -ForegroundColor Yellow
            Write-Host "$($m.Name)" -ForegroundColor White
            $i++
        }
        Write-Host ""
    }

    if ($go.Count -gt 0) {
        Write-Host "  -- OpenCode Go (5h=`$12 wk=`$30 mo=`$60) --" -ForegroundColor Cyan
        Write-Host "  Quota: https://opencode.ai/auth" -ForegroundColor Blue
        Write-Host "     Model               Cost/req   Tier       ~5h      ~Month" -ForegroundColor DarkGray
        Write-Host "     ------              ---------  ------     ------   ------" -ForegroundColor DarkGray
        foreach ($m in $go) {
            $c = $costInfo[$m.UpModel]
            if ($c) {
                $tc = $tierColor[$c.tier]
                Write-Host "    [$i] " -NoNewline -ForegroundColor Yellow
                Write-Host ("{0,-22}" -f $m.Name) -NoNewline -ForegroundColor White
                Write-Host ("${0:N4}" -f $c.cost) -NoNewline -ForegroundColor DarkGray
                Write-Host "  " -NoNewline
                Write-Host ("{0,-10}" -f $c.tier) -NoNewline -ForegroundColor $tc
                Write-Host ("{0,6}req" -f $c.limit5h) -NoNewline -ForegroundColor DarkGray
                Write-Host ("  {0,7}req" -f $c.limitMo) -ForegroundColor DarkGray
            } else {
                Write-Host "    [$i] " -NoNewline -ForegroundColor Yellow
                Write-Host $m.Name -ForegroundColor White
            }
            $i++
        }
        Write-Host ""
    }

    if ($other.Count -gt 0) {
        Write-Host "  -- Other --------------------------------" -ForegroundColor DarkYellow
        foreach ($m in $other) {
            Write-Host "    [$i] " -NoNewline -ForegroundColor Yellow
            Write-Host "$($m.Name)" -ForegroundColor White
            $i++
        }
        Write-Host ""
    }

    Write-Host "    [S] Restart shim    [Q] Quit" -ForegroundColor Yellow
    Write-Host ""
    return $ordered
}

# --- Main ---
if (-not (Resolve-Keys)) { Write-Host ""; pause; exit 1 }
try {
    Ensure-ShimRunning
    Patch-Catalog
} catch {
    Write-Host "  $_" -ForegroundColor Red
    pause
    exit 1
}

while ($true) {
    $models = Show-Menu
    if ($null -eq $models) { break }

    Write-Host "  Choose: " -NoNewline -ForegroundColor Green
    $choice = (Read-Host).Trim()

    if ($choice -eq "q" -or $choice -eq "Q") { break }

    if ($choice -eq "s" -or $choice -eq "S") {
        Invoke-Shim @("stop") 2>&1 | Out-Null
        Start-Sleep 1
        Ensure-ShimRunning
        Patch-Catalog
        Write-Host "  Shim restarted.`n" -ForegroundColor Green
        pause
        continue
    }

    $idx = 0
    if ([int]::TryParse($choice, [ref]$idx) -and $idx -ge 1 -and $idx -le $models.Count) {
        $picked = $models[$idx - 1]
        Ensure-ShimRunning
        Write-Host "`n  Switching to $($picked.Name)..." -ForegroundColor Yellow
        Invoke-Shim @("model", "use", $picked.Slug) 2>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) { throw "codex-shim model use failed." }
        Patch-Catalog

        Write-Host "`n  Launching Codex Desktop..." -ForegroundColor Yellow
        Launch-CodexApp
        break
    } else {
        Write-Host "  Invalid choice." -ForegroundColor Red
        Start-Sleep 1
    }
}
