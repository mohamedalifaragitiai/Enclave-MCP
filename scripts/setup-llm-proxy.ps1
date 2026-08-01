<#
.SYNOPSIS
    Exposes the host's loopback-bound llama-server to the DockerEngine WSL2
    distro and its containers, without restarting the LLM.

.DESCRIPTION
    llama-server runs with --host 127.0.0.1, so it is reachable from Windows but
    not from WSL2 or from containers (see HLD.md section 2 - the LLM is a shared
    external dependency, deliberately not containerised).

    Rather than rebinding the server to 0.0.0.0 - which would restart a
    GPU-resident model shared with another project, and expose it on the LAN -
    this adds a Windows portproxy listening only on the WSL-facing virtual
    adapter and forwarding to 127.0.0.1. The LLM itself is never touched and
    stays loopback-only from the LAN's point of view.

    Idempotent: safe to re-run. The WSL adapter's IP can change after a reboot
    or `wsl --shutdown`, so re-run this if the distro loses connectivity.

.NOTES
    Requires an elevated shell (netsh portproxy and firewall rule creation).
#>
[CmdletBinding()]
param(
    [int]$Port = 8001,
    [string]$LogPath = "M:\MCP_Project\.cache\tmp\llm-proxy-setup.log"
)

$ErrorActionPreference = 'Stop'

New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
Start-Transcript -Path $LogPath -Force | Out-Null

try {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must run elevated (netsh portproxy requires admin)."
    }

    # --- locate the WSL-facing virtual adapter -----------------------------
    $wsl = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.InterfaceAlias -like 'vEthernet (WSL*' } |
        Select-Object -First 1
    if (-not $wsl) {
        throw "No 'vEthernet (WSL*)' adapter found. Start the distro first: wsl -d DockerEngine -- true"
    }

    $listenAddress = $wsl.IPAddress
    $prefix        = $wsl.PrefixLength
    Write-Host "WSL adapter : $($wsl.InterfaceAlias)"
    Write-Host "listen on   : ${listenAddress}:$Port  ->  127.0.0.1:$Port"

    # --- derive the WSL subnet so the firewall rule stays narrow -----------
    $ipBytes = ([Net.IPAddress]::Parse($listenAddress)).GetAddressBytes()
    [Array]::Reverse($ipBytes)
    $ipInt   = [BitConverter]::ToUInt32($ipBytes, 0)
    $maskInt = [uint32](([math]::Pow(2, 32) - [math]::Pow(2, 32 - $prefix)))
    $netInt  = [uint32]($ipInt -band $maskInt)
    $netBytes = [BitConverter]::GetBytes($netInt)
    [Array]::Reverse($netBytes)
    $subnet = "$(([Net.IPAddress]::new($netBytes)).ToString())/$prefix"
    Write-Host "WSL subnet  : $subnet"

    # --- portproxy (drop any stale entry for this port first) --------------
    $existing = netsh interface portproxy show v4tov4 | Select-String "\s$Port\s"
    foreach ($line in $existing) {
        $fields = ($line -replace '\s+', ' ').Trim().Split(' ')
        if ($fields.Count -ge 2) {
            netsh interface portproxy delete v4tov4 `
                listenaddress=$($fields[0]) listenport=$($fields[1]) | Out-Null
            Write-Host "removed stale portproxy: $($fields[0]):$($fields[1])"
        }
    }

    netsh interface portproxy add v4tov4 `
        listenaddress=$listenAddress listenport=$Port `
        connectaddress=127.0.0.1 connectport=$Port
    if ($LASTEXITCODE -ne 0) { throw "netsh portproxy add failed ($LASTEXITCODE)" }
    Write-Host "portproxy added."

    # --- firewall: allow inbound only from the WSL subnet ------------------
    $ruleName = "MCP_Project - llama-server from WSL"
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $ruleName `
        -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $Port -RemoteAddress $subnet -Profile Any | Out-Null
    Write-Host "firewall rule created: '$ruleName' (TCP $Port from $subnet only)"

    Write-Host "`n--- active portproxy table ---"
    netsh interface portproxy show v4tov4

    Write-Host "`nRESULT: OK"
}
catch {
    Write-Host "`nRESULT: FAILED - $($_.Exception.Message)"
    throw
}
finally {
    Stop-Transcript | Out-Null
}
