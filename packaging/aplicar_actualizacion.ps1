param(
    [Parameter(Mandatory=$true)][int]$ParentId,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$SHA256,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$ReadyFile
)
$ErrorActionPreference = 'Stop'
$cacheDir = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ContabilidadAMedida\Actualizaciones'))
foreach ($item in @($Installer, $ReadyFile, $PSCommandPath)) {
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($item)) -ne $cacheDir) { throw 'Ruta fuera de la carpeta del actualizador.' }
}
$logFile = [IO.Path]::ChangeExtension($ReadyFile, '.log')
try {
    $parentProcess = Get-Process -Id $ParentId -ErrorAction Stop
    $null = $parentProcess.Handle  # conserva el handle original, evita reutilización de PID
    [IO.File]::WriteAllText($ReadyFile, 'ready')
    if (-not $parentProcess.WaitForExit(120000)) { throw 'La aplicación no se cerró a tiempo.' }
    if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $SHA256) {
        throw 'El instalador no supera la verificación de integridad.'
    }
    # Argumentos del ejecutable; nunca se evalúan como código de PowerShell.
    if ($InstallDir.Contains('"') -or $Installer.Contains('"')) { throw 'Ruta inválida.' }
    $setupArgs = @('/SILENT', '/SUPPRESSMSGBOXES', '/NOCANCEL', '/NORESTART', '/CAMUPDATE=1',
                   ('/DIR="' + $InstallDir + '"'), ('/LOG="' + $logFile + '"'))
    $setup = Start-Process -FilePath $Installer -ArgumentList $setupArgs -WindowStyle Hidden -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw ('El instalador devolvió el código ' + $setup.ExitCode) }
} catch {
    $_.Exception.Message | Out-File -LiteralPath $logFile -Encoding utf8 -Append
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        'No se pudo completar la actualización. Tus datos se conservan. Puedes volver a abrir Contabilidad a Medida e intentarlo otra vez.',
        'Contabilidad a Medida', 'OK', 'Warning') | Out-Null
} finally {
    # Rutas absolutas verificadas arriba; sin recursión ni comodines.
    Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
