<#
.SYNOPSIS
    Check the mTLS material issued by make_certs.ps1, before anything depends on it.
    Not used in the final system (broker on Ultra96). See "Mutual TLS" in Comms/README.md.

.DESCRIPTION
    A TLS handshake failure reads like a network fault -- that is the whole reason
    mqtt_config.tls_params() raises a named FileNotFoundError instead of letting the
    handshake fail. This script extends the same idea to the *content* of the
    material: every property that would otherwise first show up as a generic
    "handshake failed" on a phone, checked here where the error can name itself.

    Run it after make_certs.ps1, and again whenever a handshake starts failing --
    the first question is always whether the material is what you think it is.

.EXAMPLE
    cd comms\laptop
    .\verify_certs.ps1
#>
[CmdletBinding()]
param([string] $ApAddress = "192.168.137.1")

$ErrorActionPreference = "Stop"

$LaptopDir = $PSScriptRoot
$RepoRoot  = Split-Path $LaptopDir -Parent
$CertDir   = Join-Path $RepoRoot "certs"
$LocalDir  = Join-Path $LaptopDir "local"

$ClientRoles = @("laptop-relay", "ultra96-relay", "phone-visualiser")

$script:Failures = 0

function Test-Claim {
    param([string] $Label, [bool] $Ok, [string] $Detail = "")
    if ($Ok) {
        Write-Host ("  PASS  " + $Label) -ForegroundColor Green
    } else {
        Write-Host ("  FAIL  " + $Label) -ForegroundColor Red
        if ($Detail) { Write-Host ("        " + $Detail) -ForegroundColor DarkGray }
        $script:Failures++
    }
    if ($Ok -and $Detail) { Write-Host ("        " + $Detail) -ForegroundColor DarkGray }
}

function Get-CertText {
    param([string] $Path)
    return (& openssl x509 -in $Path -noout -text | Out-String)
}

Write-Host "verifying $CertDir`n"

# --- 1. the filenames mqtt_config.py hardcodes ----------------------------------
# These three are not free choices: mqtt_config.CA_CERT / CLIENT_CERT / CLIENT_KEY
# name them exactly. A rename here surfaces as a FileNotFoundError at relay start.

Write-Host "filenames expected by mqtt_config.py"
foreach ($f in @("ca.crt", "laptop-relay.crt", "laptop-relay.key")) {
    Test-Claim "$f exists" (Test-Path (Join-Path $CertDir $f))
}

Write-Host "`nfull inventory"
$expected = @("ca.crt", "ca.key", "broker.crt", "broker.key")
foreach ($r in $ClientRoles) { $expected += @("$r.crt", "$r.key") }
$expected += @("phone-visualiser.p12", "phone-visualiser-legacy.p12")
foreach ($f in $expected) {
    Test-Claim "$f" (Test-Path (Join-Path $CertDir $f))
}

# --- 2. chain of trust ----------------------------------------------------------
# Every leaf must verify against the CA. A leaf that does not is one the broker
# will reject with an error naming neither the file nor the reason.

Write-Host "`nchain: each leaf verifies against ca.crt"
foreach ($name in (@("broker") + $ClientRoles)) {
    $crt = Join-Path $CertDir "$name.crt"
    # No `2>&1`: in PowerShell 5.1 that wraps a native command's stderr in an
    # ErrorRecord and trips $ErrorActionPreference = "Stop", so a *failing* leaf
    # would throw here instead of being reported as a FAIL line. Exit code only.
    $out = (& openssl verify -CAfile (Join-Path $CertDir "ca.crt") $crt) | Out-String
    Test-Claim "$name.crt" ($LASTEXITCODE -eq 0) ($out.Trim())
}

# --- 3. key/cert pairing --------------------------------------------------------
# A cert paired with the wrong key fails the handshake with no useful message.
# Matching public keys is the cheap way to rule it out.

Write-Host "`npairing: each cert matches its key"
foreach ($name in (@("broker") + $ClientRoles)) {
    $fromCert = (& openssl x509 -in (Join-Path $CertDir "$name.crt") -noout -pubkey | Out-String).Trim()
    $fromKey  = (& openssl pkey -in (Join-Path $CertDir "$name.key") -pubout | Out-String).Trim()
    Test-Claim "$name.crt <-> $name.key" ($fromCert -eq $fromKey)
}

# --- 4. the SAN check -----------------------------------------------------------
# Clients connect by ADDRESS, so the broker cert needs IP SANs. A
# DNS-only cert fails hostname validation in MQTTnet and paho alike, and the
# symptom is a generic handshake error that gives no hint of the cause.
# This check catches it first.

Write-Host "`nbroker SANs are IP entries, not DNS names"
$brokerText = Get-CertText (Join-Path $CertDir "broker.crt")
$sanLine = ($brokerText -split "`n" | Select-String -Pattern "IP Address|DNS:" | ForEach-Object { $_.ToString().Trim() }) -join " "
Test-Claim "SAN contains IP Address:127.0.0.1" ($sanLine -match "IP Address:127\.0\.0\.1") $sanLine
Test-Claim "SAN contains IP Address:$ApAddress" ($sanLine -match ("IP Address:" + [regex]::Escape($ApAddress)))
Test-Claim "SAN carries no DNS-only entry" (-not ($sanLine -match "DNS:"))

# --- 5. identity: CN becomes the MQTT username ----------------------------------
# use_identity_as_username true makes the CN the username, so a wrong CN is a
# wrong identity, not a cosmetic difference. Sharing one cert across roles would
# collapse every client to one username -- the reason separate certs exist.

Write-Host "`nidentity: CN equals the role (it becomes the MQTT username)"
foreach ($role in $ClientRoles) {
    $subject = (& openssl x509 -in (Join-Path $CertDir "$role.crt") -noout -subject | Out-String).Trim()
    Test-Claim "$role" ($subject -match ("CN\s*=\s*" + [regex]::Escape($role))) $subject
}

# --- 6. extensions --------------------------------------------------------------
# A client cert offered as a server cert (or the reverse) is refused by some
# stacks and accepted by others, which is the worst combination to debug across
# three different TLS implementations.

Write-Host "`nextensions"
Test-Claim "ca.crt is a CA" ((Get-CertText (Join-Path $CertDir "ca.crt")) -match "CA:TRUE")
Test-Claim "broker.crt has serverAuth" ($brokerText -match "TLS Web Server Authentication")
Test-Claim "broker.crt is not a CA" ($brokerText -match "CA:FALSE")
foreach ($role in $ClientRoles) {
    $t = Get-CertText (Join-Path $CertDir "$role.crt")
    Test-Claim "$role.crt has clientAuth" ($t -match "TLS Web Client Authentication")
}

# --- 7. lifetime ----------------------------------------------------------------
# One year so nothing expires mid-semester. Checked because a cert that is not yet
# valid -- a clock-skew symptom -- fails exactly like an untrusted one.

Write-Host "`nlifetime"
foreach ($name in (@("ca", "broker") + $ClientRoles)) {
    $crt = Join-Path $CertDir "$name.crt"
    & openssl x509 -in $crt -noout -checkend 0 | Out-Null
    $notExpired = ($LASTEXITCODE -eq 0)
    $dates = (& openssl x509 -in $crt -noout -enddate | Out-String).Trim()
    Test-Claim "$name.crt is currently valid" $notExpired $dates
}

# --- 8. the PKCS#12 bundles Unity needs -----------------------------------------
# Unity's API level here is .NET Standard 2.1, which has no
# X509Certificate2.CreateFromPem (.NET 5+), so the phone cannot take PEM. If
# neither bundle reads back, the Unity phase is blocked before it starts.

Write-Host "`nPKCS#12 bundles readable with an empty password"
foreach ($p12 in @("phone-visualiser.p12", "phone-visualiser-legacy.p12")) {
    # -noout, not -info: -info writes the MAC parameters to stderr even on success,
    # which with `2>&1` becomes a NativeCommandError and aborts the run. The verify
    # loop above avoids the same problem.
    & openssl pkcs12 -in (Join-Path $CertDir $p12) -passin "pass:" -nokeys -noout | Out-Null
    Test-Claim $p12 ($LASTEXITCODE -eq 0)
}

# --- 9. the broker fragment -----------------------------------------------------

Write-Host "`nbroker fragment"
$tlsConf = Join-Path $LocalDir "tls.conf"
Test-Claim "local\tls.conf exists" (Test-Path $tlsConf)
if (Test-Path $tlsConf) {
    $conf = Get-Content $tlsConf -Raw
    Test-Claim "listener on 127.0.0.1" ($conf -match "listener 8883 127\.0\.0\.1")
    Test-Claim "listener on $ApAddress" ($conf -match ("listener 8883 " + [regex]::Escape($ApAddress)))
    Test-Claim "does NOT bind 0.0.0.0 (data-broker rule)" (-not ($conf -match "0\.0\.0\.0"))
    Test-Claim "require_certificate true" ($conf -match "require_certificate\s+true")
    Test-Claim "use_identity_as_username true" ($conf -match "use_identity_as_username\s+true")
    # Every path it names must exist, or mosquitto aborts at startup.
    $missing = @()
    foreach ($line in ($conf -split "`n")) {
        if ($line -match "^\s*(cafile|certfile|keyfile)\s+(.+?)\s*$") {
            if (-not (Test-Path $matches[2])) { $missing += $matches[2] }
        }
    }
    Test-Claim "every cafile/certfile/keyfile path resolves" ($missing.Count -eq 0) ($missing -join "; ")
}

# --- 10. nothing secret is committable ------------------------------------------
# This repo has a remote, and these are private keys. The absolute paths in
# tls.conf also carry the Windows username.

Write-Host "`ngit: no key material or machine-local path is committable"
Push-Location $RepoRoot
foreach ($rel in @("certs/ca.key", "certs/broker.key", "certs/laptop-relay.key",
                   "certs/phone-visualiser.p12", "laptop/local/tls.conf")) {
    & git check-ignore -q $rel
    Test-Claim "$rel is gitignored" ($LASTEXITCODE -eq 0)
}
$tracked = (& git ls-files certs laptop/local | Out-String).Trim()
$onlyPlaceholder = ($tracked -eq "") -or ($tracked -eq "laptop/local/00-placeholder.conf")
Test-Claim "nothing tracked under certs/ or laptop/local/ but the placeholder" $onlyPlaceholder $tracked
Pop-Location

Write-Host ""
if ($script:Failures -eq 0) {
    Write-Host "all checks passed" -ForegroundColor Green
    exit 0
} else {
    Write-Host "$($script:Failures) check(s) FAILED" -ForegroundColor Red
    exit 1
}
