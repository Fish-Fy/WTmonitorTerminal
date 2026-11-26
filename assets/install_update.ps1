$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath([Environment]::CurrentDirectory)
$work = [IO.Path]::GetFullPath($env:WT_UPDATE_WORK)
if ([IO.Path]::GetDirectoryName($work) -ne $root -or
    [IO.Path]::GetFileName($work) -notmatch '^\.wt-update-[a-z0-9_]+$' -or
    ([IO.File]::GetAttributes($work) -band [IO.FileAttributes]::ReparsePoint)) {
    exit 1
}
$exeName = 'WTMonitorTerminal.exe'
$exePath = Join-Path $root $exeName
$stage = Join-Path $work 'stage'
$backup = Join-Path $work 'previous'
$units = @($exeName, '_internal', 'licenses', ([string][char]0x4F7F + [char]0x7528 + [char]0x8BF4 + [char]0x660E + '.txt'))
$status = Join-Path $root '.wt-update-error.txt'
$moved = @()
$installed = @()
$lock = $null
$parentClosed = $false
$child = $null
$finish = $false

function Move-Unit([string]$source, [string]$target) {
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        try {
            if ([IO.Directory]::Exists($source)) { [IO.Directory]::Move($source, $target) }
            else { [IO.File]::Move($source, $target) }
            return
        } catch [IO.IOException] {
            if ($attempt -eq 9) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
}

function Start-Terminal([string]$ready = '') {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $exePath
    $start.WorkingDirectory = $root
    $start.UseShellExecute = $false
    $start.EnvironmentVariables.Remove('WT_UPDATE_WORK')
    $start.EnvironmentVariables.Remove('WT_UPDATE_PARENT')
    $start.EnvironmentVariables.Remove('WT_UPDATE_READY')
    if ($ready) { $start.EnvironmentVariables['WT_UPDATE_READY'] = $ready }
    return [Diagnostics.Process]::Start($start)
}

try {
    $lock = [IO.File]::Open((Join-Path $root '.wt-update.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $parent = [Diagnostics.Process]::GetProcessById([int]$env:WT_UPDATE_PARENT)
    $null = $parent.Handle
    if ([IO.File]::Exists($status)) { [IO.File]::Delete($status) }
    [IO.File]::WriteAllText((Join-Path $work 'accepted'), '')
    if (!$parent.WaitForExit(45000)) { throw 'The previous instance did not close.' }
    $parentClosed = $true

    foreach ($process in [Diagnostics.Process]::GetProcessesByName('WTMonitorTerminal')) {
        try { $runningPath = $process.MainModule.FileName } catch { continue }
        if ($runningPath -eq $exePath) {
            $parentClosed = $false  # Keep the other instance; do not start one more.
            throw 'Another instance from this folder is running.'
        }
    }
    foreach ($unit in $units) {
        $item = Join-Path $root $unit
        if ((Test-Path -LiteralPath $item) -and
            ([IO.File]::GetAttributes($item) -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'A program path is a reparse point.'
        }
    }

    $null = [IO.Directory]::CreateDirectory($backup)
    foreach ($unit in $units) {
        $old = Join-Path $root $unit
        if (Test-Path -LiteralPath $old) {
            Move-Unit $old (Join-Path $backup $unit)
            $moved += $unit
        }
    }
    # Keep the entry point absent until the rest of the new program is in place.
    foreach ($unit in @($units[1], $units[2], $units[3], $units[0])) {
        Move-Unit (Join-Path $stage $unit) (Join-Path $root $unit)
        $installed += $unit
    }
    $ready = Join-Path $work 'ready'
    $child = Start-Terminal $ready
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while (![IO.File]::Exists($ready)) {
        if ($child.WaitForExit(150)) { throw 'The new version exited before opening its window.' }
        if ([DateTime]::UtcNow -gt $deadline) { throw 'The new version did not finish starting.' }
    }
    $finish = $true
} catch {
    $problem = $_.Exception.Message
    if ($lock) {
        try {
            if ($child -and !$child.HasExited) {
                $child.Kill()
                if (!$child.WaitForExit(5000)) { throw 'The new process could not be stopped.' }
            }
            for ($i = $installed.Count - 1; $i -ge 0; $i--) {
                $unit = $installed[$i]
                Move-Unit (Join-Path $root $unit) (Join-Path $stage $unit)
            }
            for ($i = $moved.Count - 1; $i -ge 0; $i--) {
                $unit = $moved[$i]
                Move-Unit (Join-Path $backup $unit) (Join-Path $root $unit)
            }
            [IO.File]::WriteAllText($status, $problem)
            if ($parentClosed) { $null = Start-Terminal }
            $finish = $parentClosed
        } catch {
            [IO.File]::WriteAllText($status, "$problem`r`nRecovery: $backup`r`n$($_.Exception.Message)")
        }
    }
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
    if ($finish) {
        try { [IO.Directory]::Delete($work, $true) } catch {}
    }
}
