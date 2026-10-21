param(
    [Parameter(Mandatory=$true)][int]$ParentId,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$SHA256,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$ReadyFile,
    [switch]$SinDialogo
)
$ErrorActionPreference = 'Stop'
$cacheDir = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ContabilidadAMedida\Actualizaciones'))
foreach ($item in @($Installer, $ReadyFile, $PSCommandPath)) {
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($item)) -ne $cacheDir) { throw 'Ruta fuera de la carpeta del actualizador.' }
}
$logFile = [IO.Path]::ChangeExtension($ReadyFile, '.log')
$rollbackDir = $null
$archivos = @()
$instaladorIniciado = $false
$aplicacionCerrada = $false
$conservarRespaldo = $false

function ComprobarRuta([string]$Ruta) {
    $actual = [IO.Path]::GetFullPath($Ruta)
    while ($actual) {
        if ([IO.File]::Exists($actual) -or [IO.Directory]::Exists($actual)) {
            if (([IO.File]::GetAttributes($actual) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'No se permiten enlaces en las rutas de actualización.'
            }
        }
        $padre = [IO.Path]::GetDirectoryName($actual)
        if ($padre -eq $actual) { break }
        $actual = $padre
    }
}

function ArchivosPrograma([string]$Raiz) {
    ComprobarRuta $Raiz
    $pendientes = New-Object 'Collections.Generic.Stack[string]'
    $pendientes.Push($Raiz)
    while ($pendientes.Count -gt 0) {
        foreach ($item in Get-ChildItem -LiteralPath $pendientes.Pop() -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Enlace en la instalación.' }
            if ($item.PSIsContainer) {
                if ($item.Name -like 'Clientes*' -or $item.Name -eq 'Backups_Seguridad') { throw 'Hay datos de clientes dentro del programa.' }
                $pendientes.Push($item.FullName)
            } else {
                $relativa = $item.FullName.Substring($Raiz.Length).TrimStart('\')
                if ($item.Name -match '\.(db(-.*)?|sqlite.*|pdf)$' -or $item.Name -like '.env*') { throw 'Hay datos privados dentro del programa.' }
                if ($item.Extension -eq '.xlsx' -and $relativa -notin @(
                    '_internal\plantillas\Plantilla_Expediente_No_Residente_v2.xlsx',
                    '_internal\plantillas\Plantilla_Expediente_Residente_v1.xlsx')) { throw 'Hay Excel de trabajo dentro del programa.' }
                $relativa
            }
        }
    }
}

try {
    ComprobarRuta $cacheDir
    ComprobarRuta $InstallDir
    ComprobarRuta $Installer
    $parentProcess = Get-Process -Id $ParentId -ErrorAction Stop
    $null = $parentProcess.Handle  # conserva el handle original, evita reutilización de PID
    [IO.File]::WriteAllText($ReadyFile, 'ready')
    if (-not $parentProcess.WaitForExit(120000)) { throw 'La aplicación no se cerró a tiempo.' }
    $aplicacionCerrada = $true
    $algoritmo = [Security.Cryptography.SHA256]::Create()
    $archivo = [IO.File]::OpenRead($Installer)
    try {
        $huella = [BitConverter]::ToString($algoritmo.ComputeHash($archivo)).Replace('-', '').ToLowerInvariant()
    } finally {
        $archivo.Dispose()
        $algoritmo.Dispose()
    }
    if ($huella -ne $SHA256) {
        throw 'El instalador no supera la verificación de integridad.'
    }
    $InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    if (-not [IO.File]::Exists((Join-Path $InstallDir 'instalacion.ini'))) { throw 'Instalación no reconocida.' }
    # Solo se respaldan archivos del programa. Los expedientes viven fuera de este árbol.
    $archivos = @(ArchivosPrograma $InstallDir)
    $rollbackDir = Join-Path $cacheDir ('rollback-' + [Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $rollbackDir
    $archivos | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rollbackDir 'manifest.json') -Encoding UTF8
    for ($indice = 0; $indice -lt $archivos.Count; $indice++) {
        # Respaldo plano: duplicar el árbol puede superar MAX_PATH en Windows.
        $destino = Join-Path $rollbackDir ('archivo-' + $indice.ToString('D6') + '.bak')
        [IO.File]::Copy((Join-Path $InstallDir $archivos[$indice]), $destino, $false)
    }
    # Argumentos del ejecutable; nunca se evalúan como código de PowerShell.
    if ($InstallDir.Contains('"') -or $Installer.Contains('"')) { throw 'Ruta inválida.' }
    # CAMUPDATE=2: el auxiliar reinicia solo después de validar el nuevo paquete.
    $setupArgs = @('/SILENT', '/SUPPRESSMSGBOXES', '/NOCANCEL', '/NORESTART', '/CAMUPDATE=2',
                   ('/DIR="' + $InstallDir + '"'), ('/LOG="' + $logFile + '"'))
    $instaladorIniciado = $true
    $setup = Start-Process -FilePath $Installer -ArgumentList $setupArgs -WindowStyle Hidden -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw ('El instalador devolvió el código ' + $setup.ExitCode) }
    $informe = [IO.Path]::ChangeExtension($ReadyFile, '.diagnostico.json')
    $exe = Join-Path $InstallDir 'ContabilidadAMedida.exe'
    $diagnostico = Start-Process -FilePath $exe -ArgumentList @('--diagnostico-paquete', ('"' + $informe + '"')) -WindowStyle Hidden -PassThru
    if (-not $diagnostico.WaitForExit(180000)) {
        $diagnostico.Kill()
        throw 'El diagnóstico del nuevo paquete no terminó.'
    }
    $diagnostico.Refresh()
    if ($diagnostico.ExitCode -ne 0 -or -not [IO.File]::Exists($informe) -or -not (Get-Content -LiteralPath $informe -Raw | ConvertFrom-Json).ok) {
        throw 'El nuevo paquete no superó el diagnóstico.'
    }
    Start-Process -FilePath $exe -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
} catch {
    ('No se pudo completar la actualización. Tipo: ' + $_.Exception.GetType().Name) | Out-File -LiteralPath $logFile -Encoding utf8 -Append
    if ($instaladorIniciado) {
        try {
            # El programa sigue cerrado. Retira solo adiciones del árbol de código;
            # ArchivosPrograma rechaza enlaces, DB, PDF y Excel de trabajo.
            $anteriores = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
            foreach ($relativa in $archivos) { $null = $anteriores.Add($relativa) }
            foreach ($relativa in @(ArchivosPrograma $InstallDir)) {
                if (-not $anteriores.Contains($relativa)) {
                    $adicion = Join-Path $InstallDir $relativa
                    ComprobarRuta $adicion
                    Remove-Item -LiteralPath $adicion -Force
                }
            }
            for ($indice = 0; $indice -lt $archivos.Count; $indice++) {
                $relativa = $archivos[$indice]
                $destino = Join-Path $InstallDir $relativa
                ComprobarRuta $destino
                $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destino))
                [IO.File]::Copy((Join-Path $rollbackDir ('archivo-' + $indice.ToString('D6') + '.bak')), $destino, $true)
            }
            'Se restauraron los archivos de la versión anterior.' | Out-File -LiteralPath $logFile -Encoding utf8 -Append
            Start-Process -FilePath (Join-Path $InstallDir 'ContabilidadAMedida.exe') -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
        } catch {
            $conservarRespaldo = $true
            'No se pudo completar la restauración. Se conserva el respaldo del programa.' | Out-File -LiteralPath $logFile -Encoding utf8 -Append
        }
    } elseif ($aplicacionCerrada) {
        # Si no llegó a arrancar el instalador, los binarios siguen intactos.
        Start-Process -FilePath (Join-Path $InstallDir 'ContabilidadAMedida.exe') -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
    }
    if (-not $SinDialogo) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            'No se pudo completar la actualización. Tus expedientes se conservan. Revisa el registro de actualización antes de intentarlo otra vez.',
            'Contabilidad a Medida', 'OK', 'Warning') | Out-Null
    }
} finally {
    if ($rollbackDir -and -not $conservarRespaldo) {
        # Borrado individual de nuestras copias; sin borrado recursivo de rutas calculadas.
        for ($indice = 0; $indice -lt $archivos.Count; $indice++) {
            $copia = Join-Path $rollbackDir ('archivo-' + $indice.ToString('D6') + '.bak')
            ComprobarRuta $copia
            Remove-Item -LiteralPath $copia -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath (Join-Path $rollbackDir 'manifest.json') -Force -ErrorAction SilentlyContinue
        # Solo se retira el directorio exclusivo si quedó vacío.
        try { [IO.Directory]::Delete($rollbackDir, $false) } catch { }
    }
    # Rutas absolutas verificadas arriba; sin recursión ni comodines.
    Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
