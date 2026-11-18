<#
.SYNOPSIS
    Issue the mTLS material for the MQTT leg, and the broker fragment that uses it.
    Not used in the final system (broker on Ultra96). See "Mutual TLS" in Comms/README.md.

.DESCRIPTION
    A script rather than a shell-history exercise, so how each identity is issued can
    be read and re-run.

    Four keypairs, each private key staying on the machine that owns it:

        ca                  signs the other three            (this laptop, issuing only)
        broker              proves the broker to clients     (whichever host runs it)
        laptop-relay        proves the relay to the broker   (this laptop)
        ultra96-relay       proves the board to the broker   (the board)
        phone-visualiser    proves the phone to the broker   (the phone)

    Only ca.crt is distributed. No private key is ever copied to another machine
    except the one that owns it.

    Filenames are not free choices: mqtt_config.py expects exactly ca.crt,
    laptop-relay.crt and laptop-relay.key under comms/certs/.

    CN is not a free choice either: mosquitto's `use_identity_as_username true`
    turns the certificate CN into the MQTT username, so CN *is* the role name.

.PARAMETER ApAddress
    The laptop's SoftAP address, which the broker cert must cover as an IP SAN.
    Clients connect by address, and a cert carrying only DNS names
    fails hostname validation in MQTTnet and paho alike with a generic handshake
    error.

.PARAMETER Days
    Certificate lifetime. One year by default so nothing expires mid-semester.

.PARAMETER Force
    Overwrite existing material. Refuses without it, because regenerating the CA
    silently invalidates every client already provisioned -- including the cert
    already pushed to the phone.

.EXAMPLE
    cd comms\laptop
    .\make_certs.ps1
#>
[CmdletBinding()]
param(
    [string] $ApAddress = "192.168.137.1",
    [int]    $Days = 365,
    [switch] $Force
)

$ErrorActionPreference = "Stop"

$LaptopDir = $PSScriptRoot
$RepoRoot  = Split-Path $LaptopDir -Parent
$CertDir   = Join-Path $RepoRoot "certs"
$LocalDir  = Join-Path $LaptopDir "local"
$CnfDir    = Join-Path $CertDir "openssl"

# Client roles. The CN becomes the MQTT username, so these strings are a contract
# with mosquitto's ACL behaviour, not labels.
$ClientRoles = @("laptop-relay", "ultra96-relay", "phone-visualiser")

function Invoke-OpenSsl {
    # openssl reports failure through the exit code, and PowerShell 5.1 does not
    # stop on it. Every call goes through here so a failed issuance cannot be
    # mistaken for a successful one by the steps that follow.
    #
    # Deliberately a *simple* function with no param block. A param block carrying
    # [Parameter()] makes this an advanced function, which adds the common
    # parameters -- and then `-out` binds to -OutVariable/-OutBuffer instead of
    # reaching openssl, failing with "the parameter name 'out' is ambiguous".
    #
    # stderr is deliberately not redirected either: in PowerShell 5.1, `2>&1` on a
    # native executable wraps each stderr line in an ErrorRecord and trips
    # $ErrorActionPreference = "Stop", and openssl writes routine progress notes
    # to stderr on success. Let it through; the exit code is the truth.

    & openssl @args
    if ($LASTEXITCODE -ne 0) {
        throw "openssl $($args[0]) failed with exit code $LASTEXITCODE"
    }
}

function Write-TextFile {
    <#
        Always LF, never the BOM-and-CRLF that Set-Content produces. openssl reads
        these config files, and mosquitto reads tls.conf; both are happier without
        a BOM.
    #>
    param([string] $Path, [string] $Content)
    [System.IO.File]::WriteAllText($Path, ($Content -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
}

# --- guard rails ---------------------------------------------------------------

if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    throw "openssl is not on PATH."
}

if ((Test-Path (Join-Path $CertDir "ca.key")) -and (-not $Force)) {
    throw "certs\ca.key already exists. Re-issuing the CA invalidates every cert already provisioned, including the one on the phone. Pass -Force if that is what you mean."
}

New-Item -ItemType Directory -Force -Path $CertDir, $CnfDir, $LocalDir | Out-Null

Write-Host "issuing into $CertDir"
Write-Host "  openssl: $((Get-Command openssl).Source)"
Write-Host "  lifetime: $Days days"
Write-Host "  broker SANs: IP:127.0.0.1, IP:$ApAddress"
Write-Host ""

# --- config files ---------------------------------------------------------------
# Written out rather than passed as -subj/-addext so the issuing policy is a
# reviewable artefact, and so SANs cannot be silently dropped by a quoting mistake.

Write-TextFile (Join-Path $CnfDir "ca.cnf") @"
# The CA that signs every certificate on the MQTT leg. Self-signed: there is no
# public trust anchor involved, and none is wanted -- clients pin this one cert.
[req]
distinguished_name = dn
prompt             = no
x509_extensions    = v3_ca

[dn]
CN = B16 Comms CA

[v3_ca]
basicConstraints       = critical,CA:TRUE
keyUsage               = critical,keyCertSign,cRLSign
subjectKeyIdentifier   = hash
"@

# IP SANs, not DNS. Clients dial an address: loopback on the board, or the laptop's
# AP when the broker is on the laptop. A DNS-only cert fails hostname validation with a generic
# handshake error that gives no hint of the cause.
Write-TextFile (Join-Path $CnfDir "broker.cnf") @"
[req]
distinguished_name = dn
prompt             = no
req_extensions     = v3_req

[dn]
CN = b16-broker

[v3_req]
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature,keyEncipherment
extendedKeyUsage       = serverAuth
subjectAltName         = @alt

[alt]
IP.1 = 127.0.0.1
IP.2 = $ApAddress
"@

foreach ($role in $ClientRoles) {
    # CN = role because use_identity_as_username makes the CN the MQTT username.
    # Sharing one cert between roles would collapse every client to one identity.
    Write-TextFile (Join-Path $CnfDir "$role.cnf") @"
[req]
distinguished_name = dn
prompt             = no
req_extensions     = v3_req

[dn]
CN = $role

[v3_req]
basicConstraints = CA:FALSE
keyUsage         = critical,digitalSignature,keyEncipherment
extendedKeyUsage = clientAuth
"@
}

# --- the CA ---------------------------------------------------------------------

Write-Host "[1/5] CA"
Invoke-OpenSsl req -x509 -new -nodes -newkey rsa:2048 `
    -keyout (Join-Path $CertDir "ca.key") `
    -out    (Join-Path $CertDir "ca.crt") `
    -days $Days -sha256 -config (Join-Path $CnfDir "ca.cnf") | Out-Null

# --- leaf certificates ----------------------------------------------------------
# RSA 2048 throughout: Mono/IL2CPP on Android and paho both handle it without
# argument, and this is not the place to find out something does not.

function New-Leaf {
    param([string] $Name, [string] $Cnf)

    $key = Join-Path $CertDir "$Name.key"
    $csr = Join-Path $CertDir "$Name.csr"
    $crt = Join-Path $CertDir "$Name.crt"

    Invoke-OpenSsl req -new -nodes -newkey rsa:2048 -keyout $key -out $csr -config $Cnf | Out-Null
    Invoke-OpenSsl x509 -req -in $csr -CA (Join-Path $CertDir "ca.crt") -CAkey (Join-Path $CertDir "ca.key") `
        -CAcreateserial -out $crt -days $Days -sha256 `
        -extfile $Cnf -extensions v3_req | Out-Null
    Remove-Item $csr -Force
}

Write-Host "[2/5] broker"
New-Leaf -Name "broker" -Cnf (Join-Path $CnfDir "broker.cnf")

Write-Host "[3/5] client certificates"
foreach ($role in $ClientRoles) {
    Write-Host "      $role"
    New-Leaf -Name $role -Cnf (Join-Path $CnfDir "$role.cnf")
}

# --- PKCS#12 for Unity ----------------------------------------------------------
# Unity's API compatibility level here is .NET Standard 2.1, which has no
# X509Certificate2.CreateFromPem -- that is .NET 5+. So the phone cannot be handed
# a PEM key pair; it needs a PKCS#12 bundle, which `new X509Certificate2(bytes, pw)`
# can read. Empty password: the bundle is adb-pushed to the app's private storage
# and never packaged in the APK, so a passphrase compiled into the app would add a
# secret to ship without adding protection.
#
# Two variants, and testing on the phone settled which is needed: Unity's Mono refuses the modern
# bundle with "ArgumentException unsupported HMAC". The blocker is the MAC digest, not
# the encryption -- OpenSSL 3 MACs with SHA-256 and Mono's PKCS#12 reader supports only
# SHA-1. So `-macalg sha1` below is the flag that matters; -certpbe/-keypbe were
# changed at the same time and may not have been necessary.
#
# The modern bundle is still issued: it costs one call, and a future runtime that reads
# it is picked up by the loader's fallback order with no code change.

Write-Host "[4/5] PKCS#12 bundle for the phone"
Invoke-OpenSsl pkcs12 -export `
    -inkey (Join-Path $CertDir "phone-visualiser.key") `
    -in    (Join-Path $CertDir "phone-visualiser.crt") `
    -certfile (Join-Path $CertDir "ca.crt") `
    -name "phone-visualiser" `
    -out (Join-Path $CertDir "phone-visualiser.p12") -passout "pass:" | Out-Null

Invoke-OpenSsl pkcs12 -export `
    -inkey (Join-Path $CertDir "phone-visualiser.key") `
    -in    (Join-Path $CertDir "phone-visualiser.crt") `
    -certfile (Join-Path $CertDir "ca.crt") `
    -name "phone-visualiser" `
    -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1 `
    -out (Join-Path $CertDir "phone-visualiser-legacy.p12") -passout "pass:" | Out-Null

# --- the broker fragment --------------------------------------------------------
# Absolute paths, because mosquitto resolves relative ones against the working
# directory. This file is gitignored: those paths contain the Windows username,
# and mosquitto expands no environment variables, so there is nowhere else to put
# them. mosquitto.conf pulls it in with `include_dir local`.
#
# Each listener carries its own TLS options -- an included file starts in a fresh
# listener context, so options alone would fail with "requires a listener to be
# defined first".
#
# Two named listeners, mirroring the plaintext pair, NOT 0.0.0.0: the laptop must
# accept no inbound connection on any NUS-assigned address.

Write-Host "[5/5] broker fragment -> local\tls.conf"

$tlsBlock = @"
# GENERATED by laptop/make_certs.ps1 -- do not edit, do not commit.
# Machine-local: the paths below contain this machine's username.
#
# Regenerate with:  cd comms\laptop; .\make_certs.ps1 -Force
"@

foreach ($addr in @("127.0.0.1", $ApAddress)) {
    $tlsBlock += @"

listener 8883 $addr
cafile   $CertDir\ca.crt
certfile $CertDir\broker.crt
keyfile  $CertDir\broker.key
# Refuse any client that presents no certificate. Without this, TLS would encrypt
# the link but authenticate nobody, and the per-role certs would be decoration.
require_certificate true
# Make the certificate CN the MQTT username, so identity arrives with the
# connection rather than as a secret shipped inside the APK.
use_identity_as_username true
tls_version tlsv1.2
"@
}

Write-TextFile (Join-Path $LocalDir "tls.conf") $tlsBlock

Write-Host ""
Write-Host "done. next: verify_certs.ps1" -ForegroundColor Green
