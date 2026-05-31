[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$shimPath = "C:\Users\sergi\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\LocalCache\local-packages\Python312\Scripts\codex-shim.exe"
$port = 8766
$templatePath = "$HOME\.codex-shim\models.json"
$resolvedPath = "$HOME\.codex-shim\models.resolved.json"

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

function Get-ZaiQuota {
    $key = [Environment]::GetEnvironmentVariable('ZAI_API_KEY', 'User')
    if (-not $key) { return $null }
    try {
        $resp = curl.exe -s -H "Authorization: $key" -H "Accept-Language: en-US,en" -H "Content-Type: application/json" "https://api.z.ai/api/monitor/usage/quota/limit" 2>$null
        $data = $resp | ConvertFrom-Json
        if ($data.success) { return $data.data } else { return $null }
    } catch { return $null }
}

function Get-CodexQuota {
    $authPath = Join-Path $HOME ".codex" "auth.json"
    if (-not (Test-Path $authPath)) { return $null }
    try {
        $auth = Get-Content $authPath -Raw | ConvertFrom-Json
        $token = $auth.tokens.access_token
        $acctId = $auth.tokens.account_id
        if (-not $token -or -not $acctId) { return $null }
        $resp = curl.exe -s -H "Authorization: Bearer $token" -H "Accept: application/json" -H "ChatGPT-Account-Id: $acctId" -H "Origin: https://chatgpt.com" -H "Referer: https://chatgpt.com/" -H "User-Agent: Mozilla/5.0" "https://chatgpt.com/backend-api/wham/usage" 2>$null
        $data = $resp | ConvertFrom-Json
        if ($data.rate_limit) { return $data } else { return $null }
    } catch { return $null }
}

function Format-ResetTime($ts) {
    $dt = [DateTimeOffset]::FromUnixTimeMilliseconds($ts).DateTime
    $diff = $dt - (Get-Date)
    if ($diff.TotalHours -gt 24) { return "{0}d {1}h" -f [math]::Floor($diff.TotalDays), $diff.Hours }
    return "{0}h {1}m" -f [math]::Floor($diff.TotalHours), $diff.Minutes
}

function Format-PercentBar($pct) {
    $filled = [math]::Floor($pct / 10)
    $empty = 10 - $filled
    $bar = ("#" * $filled) + ("-" * $empty)
    if ($pct -ge 90) { $color = "Red" }
    elseif ($pct -ge 60) { $color = "Yellow" }
    else { $color = "Green" }
    return @{ bar = $bar; color = $color }
}

function Resolve-Keys {
    $raw = [System.IO.File]::ReadAllText($templatePath)
    $missing = @()
    foreach ($match in [regex]::Matches($raw, '%%(\w+)%%')) {
        $varName = $match.Groups[1].Value
        $val = [Environment]::GetEnvironmentVariable($varName, "User")
        if (-not $val) { $missing += $varName } else { $raw = $raw.Replace("%%$varName%%", $val) }
    }
    if ($missing.Count -gt 0) {
        Write-Host "  Missing env vars: $($missing -join ', ')" -ForegroundColor Red
        foreach ($v in $missing) { Write-Host "    setx $v `"your-key`"" -ForegroundColor White }
        Write-Host ""
        Write-Host "  Enter key for $($missing[0]) (or Enter to skip): " -NoNewline -ForegroundColor Green
        $input_key = Read-Host
        if ($input_key) { setx $missing[0] $input_key | Out-Null; Write-Host "  Saved! Restart launcher." -ForegroundColor Green }
        return $false
    }

    # Inject live Z.AI quota into display names
    $zaiQ = Get-ZaiQuota
    $zaiTag = ""
    if ($zaiQ) {
        $level = $zaiQ.level.Substring(0,1).ToUpper() + $zaiQ.level.Substring(1).ToLower()
        $tok5h = ($zaiQ.limits | Where-Object { $_.type -eq "TOKENS_LIMIT" -and $_.unit -eq 3 }).percentage
        $week  = ($zaiQ.limits | Where-Object { $_.type -eq "TOKENS_LIMIT" -and $_.unit -eq 6 }).percentage
        $zaiTag = " ($level | 5h:${tok5h}% wk:${week}%)"
    }
    $raw = $raw -replace '"display_name": "Z\.AI GLM 5\.1"', "`"display_name`": `"Z.AI Pro | GLM 5.1$zaiTag`""
    $raw = $raw -replace '"display_name": "Z\.AI GLM 5"', "`"display_name`": `"Z.AI Pro | GLM 5$zaiTag`""

    # Inject live Codex/ChatGPT quota into GPT-5.5 display name
    $codexQ = Get-CodexQuota
    $codexTag = ""
    if ($codexQ) {
        $plan = $codexQ.plan_type.ToUpper()
        $pri = $codexQ.rate_limit.primary_window
        $sec = $codexQ.rate_limit.secondary_window
        $priUsed = $pri.used_percent
        $secUsed = $sec.used_percent
        $priReset = ""
        if ($pri.reset_after_seconds) {
            $h = [math]::Floor($pri.reset_after_seconds / 3600)
            $m = [math]::Floor(($pri.reset_after_seconds % 3600) / 60)
            $priReset = " ${h}h${m}m"
        }
        $codexTag = " ($plan | 5h:${priUsed}%${priReset} wk:${secUsed}%)"
    }
    $raw = $raw -replace '"display_name": "GPT-5\.5"', "`"display_name`": `"GPT-5.5$codexTag`""

    # Inject cost info into Go display names
    foreach ($entry in $costInfo.GetEnumerator()) {
        $model = $entry.Key
        $cost = $entry.Value.cost
        $tier = $entry.Value.tier
        $oldPattern = '"display_name": "Go '
        # We do this via JSON parse instead
    }
    # Parse JSON and update Go model display names with cost
    try {
        $json = $raw | ConvertFrom-Json
        foreach ($m in $json.models) {
            if ($m.display_name -match "^Go ") {
                $c = $costInfo[$m.model]
                if ($c) {
                    $m.display_name = "Go $($m.display_name.Substring(3)) | $($c.tier) ~`$$($c.cost)/req"
                }
            }
        }
        $raw = $json | ConvertTo-Json -Depth 5
    } catch {}

    [System.IO.File]::WriteAllText($resolvedPath, $raw, (New-Object System.Text.UTF8Encoding $false))
    return $true
}

function Patch-Catalog {
    $catalogPath = "C:\Users\sergi\codex-shim\.codex-shim\custom_model_catalog.json"
    if (-not (Test-Path $catalogPath)) { return }

    $codexQ = Get-CodexQuota
    if (-not $codexQ) { return }

    $plan = $codexQ.plan_type.ToUpper()
    $pri = $codexQ.rate_limit.primary_window
    $sec = $codexQ.rate_limit.secondary_window
    $priUsed = $pri.used_percent
    $secUsed = $sec.used_percent
    $priReset = ""
    if ($pri.reset_after_seconds) {
        $h = [math]::Floor($pri.reset_after_seconds / 3600)
        $m = [math]::Floor(($pri.reset_after_seconds % 3600) / 60)
        $priReset = " ${h}h${m}m"
    }
    $newName = "GPT-5.5 ($plan | 5h:${priUsed}%${priReset} wk:${secUsed}%)"

    $raw = [System.IO.File]::ReadAllText($catalogPath)
    $raw = $raw.Replace('"display_name": "GPT-5.5"', "`"display_name`": `"$newName`"")
    [System.IO.File]::WriteAllText($catalogPath, $raw, (New-Object System.Text.UTF8Encoding $false))
}

function Ensure-ShimRunning {
    $status = & $shimPath --settings $resolvedPath --port $port status 2>&1
    if ($status -match "running") { return }
    Write-Host "`n  Starting shim on port $port..." -ForegroundColor Yellow
    & $shimPath --settings $resolvedPath --port $port start 2>&1 | ForEach-Object { Write-Host "  $_" }
    Start-Sleep -Milliseconds 500
}

function Get-Models {
    $output = & $shimPath --settings $resolvedPath --port $port model list 2>&1
    $models = @()
    foreach ($line in $output) {
        if ($line -match '^(\S+)\s+(.+?)\s+->\s+(\S+)\s+\((\w[\w-]*)\)$') {
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

function Show-Menu {
    Clear-Host
    Write-Host ""
    Write-Host "  ========================================" -ForegroundColor Cyan
    Write-Host "       CODEX-SHIM  MODEL  LAUNCHER" -ForegroundColor Cyan
    Write-Host "  ========================================" -ForegroundColor Cyan
    Write-Host ""

    # --- Z.AI Live Quota ---
    $zaiQ = Get-ZaiQuota
    if ($zaiQ) {
        $level = $zaiQ.level.ToUpper()
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
    if ($codexQ) {
        $plan = $codexQ.plan_type.ToUpper()
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
Ensure-ShimRunning
Patch-Catalog

while ($true) {
    $models = Show-Menu
    if ($null -eq $models) { break }

    Write-Host "  Choose: " -NoNewline -ForegroundColor Green
    $choice = (Read-Host).Trim()

    if ($choice -eq "q" -or $choice -eq "Q") { break }

    if ($choice -eq "s" -or $choice -eq "S") {
        & $shimPath --settings $resolvedPath --port $port stop 2>&1 | Out-Null
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
        Patch-Catalog
        Write-Host "`n  Switching to $($picked.Name)..." -ForegroundColor Yellow
        & $shimPath --settings $resolvedPath --port $port model use $picked.Slug 2>&1 | ForEach-Object { Write-Host "  $_" }

        Write-Host "`n  Launching Codex Desktop..." -ForegroundColor Yellow
        & $shimPath --settings $resolvedPath --port $port app . 2>&1 | ForEach-Object { Write-Host "  $_" }
        break
    } else {
        Write-Host "  Invalid choice." -ForegroundColor Red
        Start-Sleep 1
    }
}
