# Значок панели в трее: поднимает сервер, если порт свободен; меню «Открыть», «Скопировать адрес», «Перезапустить», «Закрыть панель».
# Один экземпляр (мьютекс); второй запуск только открывает страницу (-Open). Запуск — start.cmd.
param([switch]$Open)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$port = 8770
try { $port = [int](& python -c "import sys; sys.path.insert(0, r'$here'); from monitor.core import load_config; print(load_config().get('port', 8770))") } catch { }
$url = "http://127.0.0.1:$port/"

$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\claude-task-monitor-tray', [ref]$created)
if (-not $created) { if ($Open) { Start-Process $url }; return }

function Get-ServerPid {
    $c = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { $c.OwningProcess } else { $null }
}
function Start-Server {
    if (-not (Get-ServerPid)) { Start-Process pythonw -ArgumentList 'server.py' -WorkingDirectory $here -WindowStyle Hidden }
}
function Stop-Server {
    $p = Get-ServerPid
    if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
}

# значок: скруглённый квадрат со строками списка; зелёный — сервер жив, серый — нет
function New-Icon([string]$color) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $r = 8; $path.AddArc(1, 1, $r, $r, 180, 90); $path.AddArc(31 - $r, 1, $r, $r, 270, 90)
    $path.AddArc(31 - $r, 31 - $r, $r, $r, 0, 90); $path.AddArc(1, 31 - $r, $r, $r, 90, 90); $path.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($color))), $path)
    $w = [System.Drawing.Brushes]::White
    foreach ($y in 8, 14, 20) { $g.FillEllipse($w, 7, $y, 4, 4); $g.FillRectangle($w, 13, $y + 1, 12, 3) }
    $g.Dispose()
    [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$iconOn = New-Icon '#2e8b57'; $iconOff = New-Icon '#8a8a84'

$tray = New-Object System.Windows.Forms.NotifyIcon
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$miOpen = $menu.Items.Add('Открыть панель')
$miOpen.Font = New-Object System.Drawing.Font($miOpen.Font, [System.Drawing.FontStyle]::Bold)
$miCopy = $menu.Items.Add('Скопировать адрес')
$miRestart = $menu.Items.Add('Перезапустить сервер')
[void]$menu.Items.Add('-')
$miQuit = $menu.Items.Add('Закрыть панель')
$tray.ContextMenuStrip = $menu

function Update-State {
    $alive = [bool](Get-ServerPid)
    $tray.Icon = if ($alive) { $iconOn } else { $iconOff }
    $tray.Text = if ($alive) { "Панель задач — $url" } else { 'Панель задач — сервер остановлен' }
}
$miOpen.add_Click({ Start-Server; Start-Process $url })
$tray.add_MouseClick({ param($s, $e) if ($e.Button -eq 'Left') { Start-Server; Start-Process $url } })
$miCopy.add_Click({ [System.Windows.Forms.Clipboard]::SetText($url) })
$miRestart.add_Click({ Stop-Server; Start-Sleep -Milliseconds 500; Start-Server; Start-Sleep -Seconds 1; Update-State })
$miQuit.add_Click({ Stop-Server; $tray.Visible = $false; [System.Windows.Forms.Application]::Exit() })

Start-Server
$tray.Visible = $true
Update-State
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.add_Tick({ Update-State })
$timer.Start()
if ($Open) {   # страница — когда сервер встал
    for ($i = 0; $i -lt 20 -and -not (Get-ServerPid); $i++) { Start-Sleep -Milliseconds 250 }
    Update-State; Start-Process $url
}
[System.Windows.Forms.Application]::Run()
$tray.Dispose()
$mutex.ReleaseMutex()
