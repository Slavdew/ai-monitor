# САМОПРОВЕРКА механики очереди: скрипт не докладывает состояние, а сам
# захватывает и отпускает очередь, взводит и снимает стоп-кран, пишет и чистит
# объявления. Работает в изолированной ИБ "test\queue-check" - боевую очередь
# и доску не трогает. Состояние очереди смотреть через session-board/scripts/Show-Board.ps1.
#
# Покрывает: захват, ожидание, резервацию, снятие после падения держателя,
# стоп-кран (взведённый до входа и взведённый, пока сессия ждёт).
$ErrorActionPreference = 'Stop'
$q = Join-Path $PSScriptRoot "_queue.ps1"
. $q
$ib = "test\queue-check"
Remove-Item (Get-IbQueuePath $ib ".lock"), (Get-IbQueuePath $ib ".log"),
            (Get-IbQueuePath $ib ".stop"), (Get-IbQueuePath $ib ".board") -ErrorAction SilentlyContinue

# 1. держатель: занимает на 4 с, уходя резервирует 6 с под "проверку"
$holder = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList @(
  "-NoProfile", "-Command", ". '$q'; `$h = Enter-IbQueue '$ib' 'держатель'; Start-Sleep 4; Exit-IbQueue `$h -ReserveSec 6")

Start-Sleep -Seconds 1
$t = [Diagnostics.Stopwatch]::StartNew()
$mine = Enter-IbQueue $ib "ожидающий" 60
$t.Stop()
Exit-IbQueue $mine
$waited = [int]$t.Elapsed.TotalSeconds
"ждал $waited с (ожидание ~9: 3 с держатель + 6 с резерв)"
if ($waited -lt 8) { throw "ПРОВАЛ: не дождался резервации" }

# 2. упавший держатель очередь не держит
$killed = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList @(
  "-NoProfile", "-Command", ". '$q'; Enter-IbQueue '$ib' 'падающий' | Out-Null; Start-Sleep 30")
Start-Sleep -Seconds 2
Stop-Process -Id $killed.Id -Force
$t = [Diagnostics.Stopwatch]::StartNew()
Exit-IbQueue (Enter-IbQueue $ib "после падения" 30)
$t.Stop()
"после убитого держателя вошёл за $([int]$t.Elapsed.TotalSeconds) с"
if ($t.Elapsed.TotalSeconds -gt 6) { throw "ПРОВАЛ: лок повис после падения процесса" }

# 3. стопор, взведённый до входа: свободная очередь не спасает
Set-IbStop $ib "проверка стопора" "QueueSelfTest" | Out-Null
$blocked = $false
try { Enter-IbQueue $ib "при стопоре" 10 | Out-Null } catch { $blocked = "$_" -like "*СТОП*" }
if (-not $blocked) { throw "ПРОВАЛ: стопор не остановил вход в свободную очередь" }
Clear-IbStop $ib
Exit-IbQueue (Enter-IbQueue $ib "после снятия стопора" 10)
"стопор на входе: остановил и после снятия пропустил"
$stopNote = @(Get-IbNotes $ib | Where-Object Key -eq 'stop')
if ($stopNote.Count -ne 1 -or $stopNote[0].Text -notlike "СТОП снят*") { throw "ПРОВАЛ: снятие стопора не заменило объявление о нём" }
"стопор: снятие заменило объявление «СТОП взведён»"

# 4. стопор, взведённый пока сессия стоит в очереди за чужим оборотом
$verdict = Join-Path $env:TEMP "queue-check-waiter.txt"
Remove-Item $verdict -ErrorAction SilentlyContinue
$holder = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList @(
  "-NoProfile", "-Command", ". '$q'; `$h = Enter-IbQueue '$ib' 'долгий оборот'; Start-Sleep 20; Exit-IbQueue `$h")
Start-Sleep -Seconds 1
$waiter = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList @(
  "-NoProfile", "-Command", ". '$q'; try { Enter-IbQueue '$ib' 'ждущий' 60 | Out-Null; `$r = 'ВОШЁЛ' } " +
                            "catch { `$r = `$_.Exception.Message }; Set-Content '$verdict' -Value `$r -Encoding UTF8")
Start-Sleep -Seconds 4
Set-IbStop $ib "стопор во время ожидания" "QueueSelfTest" | Out-Null
if (-not $waiter.WaitForExit(20000)) { Stop-Process -Id $waiter.Id -Force; throw "ПРОВАЛ: ждущий не заметил стопор" }
$answer = (Get-Content $verdict -Raw -Encoding UTF8).Trim()
if ($answer -notlike "*СТОП*") { throw "ПРОВАЛ: ждущий получил '$answer' вместо стопора" }
"стопор в ожидании: ждущий вышел, не дождавшись чужого оборота"
Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue
Clear-IbStop $ib
Remove-Item $verdict -ErrorAction SilentlyContinue

# 5. доска объявлений: видна на входе в очередь (Write-Host → information stream)
#    целиком — только блокирующие, остальные одной строкой-счётчиком
Clear-IbNotes $ib
$env:CLAUDE_CODE_SESSION_ID = "selftest-writer"
Add-IbNote $ib "проверка доски" "QueueSelfTest" 1 -Key ib | Out-Null
Add-IbNote $ib "обычное объявление" "QueueSelfTest" 1 | Out-Null
$env:CLAUDE_CODE_SESSION_ID = "selftest-reader"
$seen = Join-Path $env:TEMP "queue-check-board.txt"
$entered = Enter-IbQueue $ib "с объявлением" 10 6> $seen
Exit-IbQueue $entered
$shown = Get-Content $seen -Raw -Encoding UTF8
if ($shown -notlike "*проверка доски*") { throw "ПРОВАЛ: блокирующее объявление не показано на входе в очередь" }
if ($shown -like "*обычное объявление*" -or $shown -notlike "*ещё объявлений на доске: 1*") {
  throw "ПРОВАЛ: обычное объявление на входе в очередь должно уйти в счётчик"
}
"доска: на входе в очередь блокирующее целиком, остальное счётчиком"
Remove-Item $seen -ErrorAction SilentlyContinue

# 6. срез последних, закрытие, свои объявления, пометка устаревших
Clear-IbNotes $ib
$env:CLAUDE_CODE_SESSION_ID = "selftest-writer"
Add-IbNote $ib "ветка v1" "T" -Key branch | Out-Null
Add-IbNote $ib "старое без ключа" "T" | Out-Null
Add-IbNote $ib "ветка v2" "T" -Key branch | Out-Null
Add-IbNote $ib "база закрыта" "U" -Key ib | Out-Null
Add-IbNote $ib "до закрытия" "V" | Out-Null
Add-IbNote $ib "закрыто" "V" 0 -Key close | Out-Null    # закрытие уже истекло, но снятое не вернуть
Start-Sleep -Milliseconds 20
$texts = @(Get-IbNotes $ib | ForEach-Object Text)
if ($texts -contains "ветка v1" -or $texts -notcontains "ветка v2") { throw "ПРОВАЛ: ключ не заместил прежнее объявление" }
if ($texts -notcontains "старое без ключа") { throw "ПРОВАЛ: объявление без ключа не должно замещаться" }
if ($texts -contains "до закрытия") { throw "ПРОВАЛ: истёкшее закрытие вернуло снятое объявление" }
if (@(Get-IbNotes $ib -All | ForEach-Object Text) -notcontains "ветка v1") { throw "ПРОВАЛ: -All не показал историю" }
if (@(Get-IbNotes $ib | Where-Object { -not $_.Mine }).Count) { throw "ПРОВАЛ: свои объявления не опознаны" }
$env:CLAUDE_CODE_SESSION_ID = "selftest-reader"
if (@(Get-IbNotes $ib | Where-Object Mine).Count) { throw "ПРОВАЛ: чужие объявления приняты за свои" }
$stale = Format-IbNote (Get-IbNotes $ib | Where-Object Key -eq 'ib') $ib
if ($stale -notlike "*вероятно, устарело*") { throw "ПРОВАЛ: «база закрыта» при свободной очереди не помечено" }
$env:CLAUDE_CODE_SESSION_ID = "selftest-closer"
Add-IbNote $ib "задача сдана" "T" -Key close | Out-Null
$texts = @(Get-IbNotes $ib | ForEach-Object Text)
if ($texts -contains "ветка v2" -or $texts -contains "старое без ключа" -or $texts -notcontains "задача сдана") {
  throw "ПРОВАЛ: закрытие не сняло объявления тега"
}
"доска: срез последних, закрытие, свои объявления и пометка устаревших работают"

# тег по номеру задачи: разные подписи одной задачи — один тег
Add-IbNote $ib "номер-v1" "задача 123456 переверстка" -Key branch | Out-Null
Add-IbNote $ib "номер-v2" "123456" -Key branch | Out-Null
$texts = @(Get-IbNotes $ib | ForEach-Object Text)
if ($texts -contains "номер-v1" -or $texts -notcontains "номер-v2") { throw "ПРОВАЛ: подписи одной задачи не сведены к номеру" }
if ((Get-IbTaskNumber "feature/123456-svodka") -ne "123456" -or (Get-IbTaskNumber "feature/000000-x")) {
  throw "ПРОВАЛ: номер задачи из ветки разобран неверно"
}
"доска: подписи одной задачи сводятся к номеру"

# 7. хук session-board.js на той же доске: читатель видит срез, писатель — не своё
$hook = Join-Path $PSScriptRoot "..\hooks\session-board.js"
$proj = Join-Path $env:TEMP "queue-check-project"
New-Item -ItemType Directory -Path $proj -Force | Out-Null
Set-Content (Join-Path $proj ".dev.env") -Value "ONEC_IB_SERVER=$ib" -Encoding ASCII
$env:CLAUDE_PROJECT_DIR = $proj
$reader = ('{"session_id":"selftest-reader"}' | node $hook) -join "`n"
$writer = ('{"session_id":"selftest-writer"}' | node $hook) -join "`n"
Remove-Item Env:\CLAUDE_PROJECT_DIR, Env:\CLAUDE_CODE_SESSION_ID
Remove-Item $proj -Recurse -Force
if ($reader -notlike "*база закрыта (вероятно, устарело*" -or $reader -notlike "*задача сдана*" -or
    $reader -like "*ветка v2*" -or $reader -like "*до закрытия*" -or
    $reader -like "*номер-v1*" -or $reader -notlike "*номер-v2*") {
  throw "ПРОВАЛ: хук показал читателю не срез:`n$reader"
}
if ($writer -notlike "*задача сдана*" -or $writer -like "*база закрыта*") {
  throw "ПРОВАЛ: хук показал писателю его же объявления:`n$writer"
}
"хук: срез и свои объявления — как в PowerShell"

# 8. подпись с переводом строки, табуляцией и # не ломает доску и не подменяет ключ;
#    испорченную строку (так пишет старая версия скилла) читатели пропускают
Clear-IbNotes $ib
$env:CLAUDE_CODE_SESSION_ID = "selftest-writer"
Add-IbNote $ib "грязная подпись" "мусор`r`na`tb #close" -Key bot | Out-Null
Add-Content (Get-IbQueuePath $ib ".board") -Value "мусор`ta`tb`tот старой версии" -Encoding UTF8
Set-IbStop $ib "грязный стопор" "задача 123456`n`t#ib" | Out-Null
Clear-IbStop $ib
$notes = @(Get-IbNotes $ib)
$dirty = @($notes | Where-Object Text -eq "грязная подпись")
if ($dirty.Count -ne 1 -or $dirty[0].Tag -ne "мусор a b close" -or $dirty[0].Key -ne "bot") {
  throw "ПРОВАЛ: подпись не очищена:`n$($notes | Out-String)"
}
$stopNote = @($notes | Where-Object Key -eq 'stop')
if ($stopNote.Count -ne 1 -or $stopNote[0].Text -notlike "СТОП снят*") { throw "ПРОВАЛ: стопор с грязной подписью не снят с доски" }
New-Item -ItemType Directory -Path $proj -Force | Out-Null
Set-Content (Join-Path $proj ".dev.env") -Value "ONEC_IB_SERVER=$ib" -Encoding ASCII
$env:CLAUDE_PROJECT_DIR = $proj
$reader = ('{"session_id":"selftest-dirty"}' | node $hook) -join "`n"
Remove-Item Env:\CLAUDE_PROJECT_DIR, Env:\CLAUDE_CODE_SESSION_ID
Remove-Item $proj -Recurse -Force
if ($reader -notlike "*грязная подпись*" -or $reader -notlike "*СТОП снят*") { throw "ПРОВАЛ: хук замолчал на испорченной строке:`n$reader" }
"подпись: перевод строки, табуляция и # вычищены, испорченную строку пропускают и PowerShell, и хук"

# 9. версия формата: писатель ставит свою и не понижает чужую, отставший читатель предупреждает
$version = Get-IbQueuePath $ib ".board.version"
if ((Get-Content $version -Raw) -ne "$BoardFormat") { throw "ПРОВАЛ: писатель не отметил версию формата доски" }
if (Get-IbBoardFormatWarning $ib) { throw "ПРОВАЛ: предупреждение о версии при совпадающем формате" }
Set-Content $version -Value ($BoardFormat + 1) -Encoding ASCII -NoNewline     # доску пишет версия новее
Add-IbNote $ib "из будущего" "T" | Out-Null
if ((Get-Content $version -Raw) -ne "$($BoardFormat + 1)") { throw "ПРОВАЛ: писатель понизил версию формата" }
if (-not (Get-IbBoardFormatWarning $ib)) { throw "ПРОВАЛ: отставшая версия не предупредила" }
New-Item -ItemType Directory -Path $proj -Force | Out-Null
Set-Content (Join-Path $proj ".dev.env") -Value "ONEC_IB_SERVER=$ib" -Encoding ASCII
$env:CLAUDE_PROJECT_DIR = $proj
$fresh = ('{"session_id":"selftest-fresh"}' | node $hook) -join "`n"
Remove-Item Env:\CLAUDE_PROJECT_DIR
Remove-Item $proj -Recurse -Force
if ($fresh -notlike "*доска в формате $($BoardFormat + 1)*") { throw "ПРОВАЛ: хук не предупредил об устаревшей версии:`n$fresh" }
"версия формата: не понижается, отставшая версия предупреждает (и хук)"

Clear-IbNotes $ib
Remove-Item (Get-IbQueuePath $ib ".cursor.*"), (Get-IbQueuePath $ib ".own.*"), $version -ErrorAction SilentlyContinue

"--- журнал ---"
Get-Content (Get-IbQueuePath $ib ".log") -Encoding UTF8
Remove-Item (Get-IbQueuePath $ib ".lock"), (Get-IbQueuePath $ib ".log"),
            (Get-IbQueuePath $ib ".stop"), (Get-IbQueuePath $ib ".board") -ErrorAction SilentlyContinue
"ПРОВЕРКА ПРОЙДЕНА"
