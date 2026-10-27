# Общая очередь к ИБ разработчика для параллельных сессий разработки.
#
# Ресурс один на всех: экспорт EDT, workspace CLI, блокировка конфигурации,
# сеансы проверки. Захват — открытие лок-файла с FileShare::Read: атомарен на
# уровне ОС (в отличие от «проверил Test-Path → создал», между чем успевает
# влезть вторая сессия), снимается вместе с процессом (упавшая сессия очередь
# не держит) и читается снаружи — видно, кто занял и зачем.
#
# Резервация закрывает разрыв между процессами: обновление и печать — разные
# процессы, и в секунды между ними чужой оборот успевает влезть и уронить сеанс
# проверки. Уходя, обновление оставляет метку RESERVED со сроком; чужой оборот,
# увидев непросроченную метку, встаёт обратно в очередь. Метка живёт по времени,
# а не по живости процесса: проверять некого — тот, кто её поставил, уже вышел.
#
# Очередь лежит в %LOCALAPPDATA%\1c-ib-queue вне репозитория, файлы именуются по
# строке подключения — несколько баз не мешают друг другу.
$QueueDir = Join-Path $env:LOCALAPPDATA "1c-ib-queue"

# Дописать строку в общий файл очереди. Add-Content при одновременной записи нескольких сессий
# падает на занятом файле (зонд 26.10: 195–197 ошибок из 200 у каждого из трёх процессов) —
# пишущие встают в очередь на именованном мьютексе файла. Мьютекс брошенного процесса ОС
# отдаёт следующему (AbandonedMutexException — запись всё равно наша).
function Add-IbLine([string]$Path, [string]$Line) {
  $name = 'Local\1c-ib-queue-' + ((Split-Path $Path -Leaf) -replace '[^\w.-]', '_')
  $mutex = New-Object System.Threading.Mutex($false, $name)
  $own = $false
  try {
    try { $own = $mutex.WaitOne(10000) } catch [System.Threading.AbandonedMutexException] { $own = $true }
    if (-not $own) { throw "файл очереди занят дольше 10 с: $Path" }
    [System.IO.File]::AppendAllText($Path, $Line + "`r`n", (New-Object System.Text.UTF8Encoding $false))
  } finally {
    if ($own) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
  }
}

function Get-IbQueuePath([string]$IbServer, [string]$Extension) {
  New-Item -ItemType Directory -Path $QueueDir -Force | Out-Null
  Join-Path $QueueDir (($IbServer -replace '\W', '_') + $Extension)
}

# Построчная история оборотов: только для наблюдения, очередью не управляет.
# Признак занятости — захваченный лок-файл, а не последняя строка журнала:
# упавшая сессия свою строку не допишет и остановила бы очередь навсегда.
function Write-IbQueueLog([string]$IbServer, [string]$Text) {
  $path = Get-IbQueuePath $IbServer ".log"
  Add-IbLine $path ("{0:yyyy-MM-dd HH:mm:ss}`t$PID`t$Text" -f (Get-Date))

  # Ротация в одно поколение: журнал нужен для разбора недавнего, а не как архив.
  # Обрезать по строкам дороже — файл пришлось бы читать целиком на каждой записи.
  if ((Get-Item $path).Length -gt 1MB) { Move-Item $path "$path.1" -Force }
}

# Курсоры прочитанного (их пишет хук session-board.js) и списки своих объявлений копятся
# по одному на сессию, а сессии не живут неделями — старые чистим, чтобы каталог очереди
# оставался читаемым. Вызывается при каждом объявлении.
function Remove-IbStaleCursors([string]$IbServer, [int]$Days = 7) {
  foreach ($ext in @(".cursor.*", ".own.*")) {
    $mask = (Split-Path (Get-IbQueuePath $IbServer $ext) -Leaf)
    Get-ChildItem $QueueDir -Filter $mask -File -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$Days) } |
      Remove-Item -Force -ErrorAction SilentlyContinue
  }
}

# Доска объявлений, общая для всех сессий. Общие параметры работы (.dev.env,
# состояние ИБ, установленное расширение, ветка) меняет одна сессия, а касаются
# они всех, и узнать об этом из своего контекста другая сессия не может.
#
# Это лента объявлений, а не переписка: у каждого свой срок, по истечении оно
# само уходит, отвечать некому. Читается там же, где уже есть события: на входе
# в очередь (начало итерации цикла) и хуком UserPromptSubmit (каждый ввод
# пользователя) — опрашивать по расписанию не нужно.
#
# Срез последних. Строка доски — TSV «когда · до · чей · текст», формат один для всех
# версий скилла. Ключ факта пишется в колонку «чей» через « #»: «задача 123456 #ib».
# Новое объявление с тем же тегом и ключом заменяет прежнее, ключ close снимает всё,
# что было по тегу раньше. Старые версии скилла видят ключ просто частью «чей» — потому
# он там, а не в новой колонке: лишнюю колонку старый разбор приклеил бы к тексту.
# Объявления без ключа (так пишут и старые версии) друг друга не заменяют.
#
# Тег — задача: если в «чей» есть номер задачи (6 цифр, кроме 000000), теги сравниваются
# по нему — «задача 123456» и «123456 переверстка» одна задача. Иначе — строкой целиком.
#
# Ключи ib, stop, branch — блокирующие: показываются целиком везде, в т. ч. на входе
# в очередь, и сверяются с реальным состоянием очереди и стопора.
$BoardBlockingKeys = @('ib', 'stop', 'branch')
$BoardCloseKey     = 'close'

# Версия формата доски. Код скилла у каждого чекаута свой, а файл доски общий для всех
# чекаутов машины — разойтись могут только данные. Писатель поднимает номер в
# <ИБ>.board.version до своего (никогда не понижает), читатель с номером ниже
# предупреждает, что его версия скилла устарела. Сам ничего не обновляет: подтянуть
# ветку — решение человека. Ломающая правка формата поднимает $BoardFormat; если старые
# версии не должны даже читать новые строки — новые пишутся в другой файл доски.
# Хук держит тот же номер (FORMAT в hooks/session-board.js).
$BoardFormat = 1

function Get-IbBoardFormatWarning([string]$IbServer) {
  $path = Get-IbQueuePath $IbServer ".board.version"
  $onDisk = if (Test-Path $path) { (Get-Content $path -Raw) -as [int] } else { 0 }
  if ($onDisk -gt $BoardFormat) {
    "Доска в формате $onDisk, эта версия скилла session-board понимает ${BoardFormat}: " +
    "объявления могут читаться неверно — обновите скилл session-board"
  }
}

function Update-IbBoardFormat([string]$IbServer) {
  $path = Get-IbQueuePath $IbServer ".board.version"
  $onDisk = if (Test-Path $path) { (Get-Content $path -Raw) -as [int] } else { 0 }
  if ($onDisk -lt $BoardFormat) { Set-Content $path -Value $BoardFormat -Encoding ASCII -NoNewline }
}

# Свои объявления сессия помнит в <ИБ>.own.<session_id> (моменты записи), а не на доске:
# так формат строки не меняется. Хук и вход в очередь их не показывают — объявивший
# своё объявление знает.
function Get-IbOwnPath([string]$IbServer) {
  $sid = $env:CLAUDE_CODE_SESSION_ID -replace '[^\w-]', ''
  if ($sid) { Get-IbQueuePath $IbServer ".own.$sid" } else { $null }
}

function Get-IbTaskNumber([string]$Text) {
  if ($Text -match '(?<!\d)(\d{6})(?!\d)' -and $Matches[1] -ne '000000') { $Matches[1] } else { $null }
}

function Get-IbTagId([string]$Tag) {
  $number = Get-IbTaskNumber $Tag
  if ($number) { $number } else { $Tag.ToLower() }
}

# Тег по умолчанию — задача из ветки текущего каталога (feature/123456-… → «задача 123456»),
# чтобы сессия не придумывала подпись заново и закрытие находило её объявления.
function Get-IbBranchOwner {
  $branch = try { git branch --show-current 2>$null } catch { "" }
  $number = Get-IbTaskNumber "$branch"
  if ($number) { "задача $number" } else { $null }
}

# Подпись пишется в колонку «чей» строки доски и в файл стопора. Перевод строки или
# табуляция в ней ломают разбор доски, а « #слово» читается как ключ (« #close» снял бы
# чужие объявления). Управляющие символы и # — в пробел; формат строки не меняется.
function ConvertTo-IbOwner([string]$Owner) {
  (($Owner -replace '[\p{Cc}#]', ' ') -replace '\s+', ' ').Trim()
}

function Add-IbNote([string]$IbServer, [string]$Text, [string]$Owner, [int]$Hours = 12, [string]$Key = "") {
  $now  = Get-Date
  $Owner = ConvertTo-IbOwner $Owner
  $who  = if ($Owner) { $Owner } else { "pid $PID" }
  if ($Key) { $who = "$who #$($Key.ToLower())" }
  $line = "{0}`t{1}`t{2}`t{3}" -f $now.ToBinary(), $now.AddHours($Hours).ToBinary(), $who, ($Text -replace '\s+', ' ')
  Add-IbLine (Get-IbQueuePath $IbServer ".board") $line
  Update-IbBoardFormat $IbServer
  $own = Get-IbOwnPath $IbServer
  if ($own) { Add-IbLine $own ([string]$now.ToBinary()) }
  Write-IbQueueLog $IbServer "объявление ($who): $Text"
  Remove-IbStaleCursors $IbServer
  $now
}

# Непросроченные объявления; $Since отсекает уже прочитанные этой сессией.
# Замещённые и снятые закрытием отбрасываются; -All показывает и их (история).
# Замещение считается и по просроченным строкам: иначе истёкшее закрытие вернуло
# бы на доску то, что оно сняло.
function Get-IbNotes([string]$IbServer, [datetime]$Since = [datetime]::MinValue, [switch]$All) {
  $path = Get-IbQueuePath $IbServer ".board"
  if (-not (Test-Path $path)) { return @() }
  $own = Get-IbOwnPath $IbServer
  $mine = if ($own -and (Test-Path $own)) { @(Get-Content $own -Encoding ASCII) } else { @() }

  $notes = foreach ($line in (Get-Content $path -Encoding UTF8)) {
    $parts = $line -split "`t", 4
    if ($parts.Count -lt 4) { continue }
    # Испорченную строку (подпись с переводом строки от старой версии скилла) пропускаем:
    # иначе падают чтение доски и вход в очередь у всех сессий. try ловит и не число,
    # и число вне диапазона дат.
    $at, $until = try { [datetime]::FromBinary($parts[0]), [datetime]::FromBinary($parts[1]) } catch { continue }
    $tag, $key = $parts[2], ""
    if ($parts[2] -match '^(.*\S)\s+#(\S+)$') { $tag, $key = $Matches[1], $Matches[2].ToLower() }
    [pscustomobject]@{
      At = $at; Until = $until
      Owner = $parts[2]; Tag = $tag; TagId = (Get-IbTagId $tag); Key = $key; Text = $parts[3]
      Mine = $mine -contains $parts[0]
    }
  }

  $closed = @{}; $latest = @{}
  foreach ($n in $notes) {
    if ($n.Key -eq $BoardCloseKey) { if ($n.At -gt $closed[$n.TagId]) { $closed[$n.TagId] = $n.At } }
    if ($n.Key -and $n.At -gt $latest["$($n.TagId)#$($n.Key)"]) { $latest["$($n.TagId)#$($n.Key)"] = $n.At }
  }

  $now = Get-Date
  foreach ($n in $notes) {
    if ($n.Until -lt $now -or $n.At -le $Since) { continue }
    if (-not $All) {
      if ($n.Key -and $n.At -lt $latest["$($n.TagId)#$($n.Key)"]) { continue }
      if ($n.Key -ne $BoardCloseKey -and $closed[$n.TagId] -and $n.At -lt $closed[$n.TagId]) { continue }
    }
    $n
  }
}

function Test-IbNoteBlocking($Note) { $BoardBlockingKeys -contains $Note.Key }

# Очередь к ИБ сейчас занята: лок держит живой процесс или стоит непросроченный резерв.
# Пробное открытие на запись на миг занимает лок — входящий в очередь в этот миг
# просто повторит попытку через 3 с.
function Test-IbQueueBusy([string]$IbServer) {
  $path = Get-IbQueuePath $IbServer ".lock"
  if (-not (Test-Path $path)) { return $false }
  try { [IO.File]::Open($path, 'Open', 'ReadWrite', 'ReadWrite').Dispose() } catch [IO.IOException] { return $true }
  $held = try { Get-Content $path -Raw -Encoding UTF8 } catch { "" }
  ($held -match 'RESERVED\s+(-?\d+)') -and ((Get-Date) -lt [datetime]::FromBinary([long]$Matches[1]))
}

# Пометка для блокирующего объявления, которое расходится с реальным состоянием:
# сессию могли прервать, не сняв объявление. Не скрываем — решает читающий.
function Get-IbNoteStaleMark([string]$IbServer, $Note) {
  if ($Note.Key -eq 'stop' -and -not (Get-IbStop $IbServer)) { return " (вероятно, устарело: стопор не взведён)" }
  if ($Note.Key -eq 'ib' -and -not (Get-IbStop $IbServer) -and -not (Test-IbQueueBusy $IbServer)) {
    return " (вероятно, устарело: очередь к ИБ свободна, стопор не взведён)"
  }
  ""
}

function Format-IbNote($Note, [string]$IbServer = "") {
  $mark = if ($IbServer -and (Test-IbNoteBlocking $Note)) { Get-IbNoteStaleMark $IbServer $Note } else { "" }
  "[{0:dd.MM HH:mm} {1}] {2}{3}" -f $Note.At, $Note.Owner, $Note.Text, $mark
}

function Clear-IbNotes([string]$IbServer) {
  Remove-Item (Get-IbQueuePath $IbServer ".board") -Force -ErrorAction SilentlyContinue
  Write-IbQueueLog $IbServer "доска очищена"
}

# Стоп-кран, общий для всех параллельных сессий. Пока файл <ИБ>.stop существует,
# ни одна сессия не начинает оборот к ИБ: ни обновление, ни печать. В отличие от
# очереди это не «подожди своей минуты», а «встань и дождись человека» — снимает
# стопор только он (Hold-IB.ps1 -Off).
#
# Взвести его может и пользователь, и сессия, наткнувшаяся на то, что касается
# остальных: сломанная загрузка, ИБ в неизвестном состоянии, спорная правка в
# общем объекте. Файл, а не переменная окружения, — потому что сессии живут в
# разных процессах и должны видеть стопор, взведённый уже после их старта.
function Get-IbStop([string]$IbServer) {
  $path = Get-IbQueuePath $IbServer ".stop"
  if (-not (Test-Path $path)) { return $null }
  $text = try { (Get-Content $path -Raw -Encoding UTF8).Trim() } catch { "" }
  if ($text) { $text } else { "без причины" }
}

function Set-IbStop([string]$IbServer, [string]$Reason, [string]$Owner) {
  $Owner = ConvertTo-IbOwner $Owner   # Clear-IbStop разбирает подпись из файла регуляркой
  $who = if ($Owner) { $Owner } else { "pid $PID" }
  $text = "взведён {0:yyyy-MM-dd HH:mm:ss}, $who : $Reason" -f (Get-Date)
  Set-Content -Path (Get-IbQueuePath $IbServer ".stop") -Value $text -Encoding UTF8
  Write-IbQueueLog $IbServer "СТОП взведён — $text"
  # Стопор попадает и на доску: сессия, которая сейчас не в цикле, узнает о нём
  # с ближайшего ввода пользователя, а не когда упрётся в очередь.
  Add-IbNote $IbServer "СТОП взведён: $Reason" $who 24 -Key stop | Out-Null
  $text
}

# Снятие замещает объявление о стопоре под тем же тегом: без этого доска ещё сутки
# твердила бы, что база закрыта. Тег берём из файла стопора — его ставил Set-IbStop.
function Clear-IbStop([string]$IbServer) {
  $stop = Get-IbStop $IbServer
  Remove-Item (Get-IbQueuePath $IbServer ".stop") -Force -ErrorAction SilentlyContinue
  Write-IbQueueLog $IbServer "СТОП снят"
  if ($stop -match '^взведён [^,]+, (.+?) : ') {
    Add-IbNote $IbServer "СТОП снят, к ИБ можно обращаться" ($Matches[1] -replace '\s+#\S+$', '') 2 -Key stop | Out-Null
  }
}

# Проверяется на входе в очередь и на каждой итерации ожидания: стопор, взведённый
# во время чужого долгого оборота, останавливает и того, кто уже стоит в очереди.
# Оборот, который к этому моменту уже идёт, не прерывается — обрывать загрузку
# конфигурации на середине опаснее, чем дать ей закончиться.
function Assert-IbNotStopped([string]$IbServer, [string]$What) {
  $stop = Get-IbStop $IbServer
  if (-not $stop) { return }
  Write-IbQueueLog $IbServer "$What : остановлено стопором"
  throw "СТОП: $stop`nРабота с ИБ приостановлена до команды пользователя. " +
        "Снять может только он: Hold-IB.ps1 -Off"
}

# Сквозной номер обновления ИБ: по журналу видно, какое по счёту обновление
# стоит в базе и на каком из них печатали. Инкремент идёт уже под захваченным
# локом, поэтому два оборота одинаковый номер не получат.
function Get-IbTurnNumber([string]$IbServer, [switch]$Next) {
  $path = Get-IbQueuePath $IbServer ".turn"
  $number = if (Test-Path $path) { [int](Get-Content $path -Raw) } else { 0 }
  if ($Next) { $number++; Set-Content $path -Value $number -Encoding UTF8 }
  $number
}

# Встать в очередь и дождаться доступа к ИБ. Возвращает описатель для Exit-IbQueue.
#
# $What описывает оборот для соседних сессий и должен называть задачу
# («обновление ИБ, задача 123456»): в журнале и в лок-файле видно только его.
# -Numbered — оборот меняет состояние ИБ: получает следующий номер обновления;
# без ключа оборот отмечается номером обновления, на котором он работает.
function Enter-IbQueue([string]$IbServer, [string]$What, [int]$TimeoutSec = 1800, [switch]$Numbered) {
  $path = Get-IbQueuePath $IbServer ".lock"
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $announced = $false
  Assert-IbNotStopped $IbServer $What

  while ($true) {
    $stream = $null
    try { $stream = [IO.File]::Open($path, 'OpenOrCreate', 'ReadWrite', 'Read') } catch [IO.IOException] { }

    if ($stream) {
      # Файл ничей — но, возможно, зарезервирован под чужую проверку.
      $held = (New-Object IO.StreamReader($stream)).ReadToEnd()
      $until = if ($held -match 'RESERVED\s+(-?\d+)') { [datetime]::FromBinary([long]$Matches[1]) } else { [datetime]::MinValue }
      if ((Get-Date) -ge $until) {
        $turn  = Get-IbTurnNumber $IbServer -Next:$Numbered
        $label = if ($Numbered) { "$What : обновление #$turn" } else { "$What : на обновлении #$turn" }
        $stream.SetLength(0)
        $stream.Position = 0
        $writer = New-Object IO.StreamWriter($stream)
        $writer.WriteLine("pid $PID`t$label`tс {0:HH:mm:ss}" -f (Get-Date))
        $writer.Flush()
        Write-IbQueueLog $IbServer "$label : очередь занята"
        # Начало итерации цикла — здесь же и читается доска. Write-Host, а не
        # вывод в поток: он ушёл бы в возвращаемое значение функции. Целиком —
        # только блокирующие: остальное сессия уже видела через хук, а цикл
        # печати повторял бы всю доску на каждом обороте.
        $formatWarning = Get-IbBoardFormatWarning $IbServer
        if ($formatWarning) { Write-Host "  ВНИМАНИЕ: $formatWarning" }
        $notes = @(Get-IbNotes $IbServer | Where-Object { -not $_.Mine })
        $blocking = @($notes | Where-Object { Test-IbNoteBlocking $_ })
        foreach ($note in $blocking) { Write-Host "  ОБЪЯВЛЕНИЕ $(Format-IbNote $note $IbServer)" }
        if ($notes.Count -gt $blocking.Count) {
          Write-Host "  ещё объявлений на доске: $($notes.Count - $blocking.Count) (Show-Board.ps1 -Board)"
        }
        return [pscustomobject]@{ Stream = $stream; Writer = $writer; IbServer = $IbServer; What = $label; Turn = $turn }
      }
      $stream.Dispose()
      $holder = "проверка предыдущего оборота, резерв до {0:HH:mm:ss}" -f $until
    } else {
      $raw = try { Get-Content $path -Raw -Encoding UTF8 } catch { "" }
      $holder = if ($raw) { $raw.Trim() } else { "неизвестно" }
    }

    if ((Get-Date) -ge $deadline) { throw "Очередь: ИБ не освободилась за $TimeoutSec с. Держит: $holder" }
    if (-not $announced) { Write-IbQueueLog $IbServer "$What : ждёт очереди, держит: $holder"; $announced = $true }
    Start-Sleep -Seconds 3
    Assert-IbNotStopped $IbServer $What
  }
}

# ReserveSec > 0 — придержать очередь под проверку, которую запустит следующий
# процесс этой же сессии (печать сразу после обновления).
function Exit-IbQueue($Queue, [int]$ReserveSec = 0) {
  if ($ReserveSec -gt 0) {
    $Queue.Stream.SetLength(0)
    $Queue.Stream.Position = 0
    $Queue.Writer.WriteLine("RESERVED {0}`tpid $PID`tпроверка после обновления" -f (Get-Date).AddSeconds($ReserveSec).ToBinary())
    $Queue.Writer.Flush()
  }
  $Queue.Stream.Dispose()
  $tail = if ($ReserveSec -gt 0) { ", резерв $ReserveSec с под проверку" } else { "" }
  Write-IbQueueLog $Queue.IbServer "$($Queue.What) : очередь освобождена$tail"
}
