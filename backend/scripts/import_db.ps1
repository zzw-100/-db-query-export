[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$mysql = 'D:\mysql\mysql\bin\mysql.exe'
# Avoid hardcoding non-ASCII paths: derive from script location
$scripts = $PSScriptRoot
$files = @(
    'create_interview_db.sql',
    'seed_interview_data.sql',
    'seed_interview_data_part2.sql',
    'seed_interview_data_part3.sql'
)

function Read-SqlFileSmart {
    param([string]$Path)
    # Detect encoding: strict UTF-8 first, fallback to GBK
    $raw = [System.IO.File]::ReadAllBytes($Path)
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        return $strictUtf8.GetString($raw)
    } catch {
        return [System.Text.Encoding]::GetEncoding('GBK').GetString($raw)
    }
}

foreach ($f in $files) {
    Write-Host "Importing $f ..."
    $src = Join-Path $scripts $f
    $tmp = Join-Path $env:TEMP $f
    # Source files have mixed encodings (UTF-8 / GBK); normalize to UTF-8 for mysql
    $content = Read-SqlFileSmart -Path $src
    [System.IO.File]::WriteAllText($tmp, $content, (New-Object System.Text.UTF8Encoding $false))

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $mysql
    $psi.Arguments = '-u root --default-character-set=utf8mb4'
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $p = [System.Diagnostics.Process]::Start($psi)
    $bytes = [System.IO.File]::ReadAllBytes($tmp)
    $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $p.StandardInput.Close()
    $err = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    if ($p.ExitCode -ne 0) {
        Write-Host "FAILED: $f"
        Write-Host $err
        exit 1
    }
    Write-Host "OK: $f"
}

& $mysql -u root --default-character-set=utf8mb4 -e "SELECT id, first_name, current_company FROM interview_db.candidates LIMIT 3;"
Write-Host "DONE"
