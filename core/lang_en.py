"""English translations for Shinra Tensei.

Keys are the Russian source strings exactly as they appear in the code (tr("...")).
Placeholders {name} / {0} must stay the same as in the key.
"""

EN = {
    'Завершаю экспорт: дописываю структуру PST-файлов…':
        'Finishing export: writing the structure of PST files…',
    'Сейчас идёт {joined}. Остановить и выйти?\nУже скачанные письма сохранятся, при следующем запуске загрузка продолжится. Уже записанные в PST письма останутся в файле.':
        '{joined} is in progress. Stop and exit?\nMessages already downloaded are kept; the download will resume next time. Messages already written to PST stay in the file.',
    'Выход':
        'Exit',
    ' и ':
        ' and ',
    'экспорт в PST':
        'PST export',
    'загрузка почты':
        'mail download',
    'Остановить ящик':
        'Stop mailbox',
    'Повторить':
        'Retry',
    'Пропускать все ошибки этого ящика':
        'Skip all errors for this mailbox',
    'Пропустить':
        'Skip',
    "<b>{0}</b><br>Папка: {1}<br>UID письма: {2}<br><br><span style='color:{err_color}'>{3}</span><br><br>Пропустить это письмо и продолжить загрузку?":
        "<b>{0}</b><br>Folder: {1}<br>Message UID: {2}<br><br><span style='color:{err_color}'>{3}</span><br><br>Skip this message and continue downloading?",
    'Ошибка при загрузке письма':
        'Error while downloading a message',
    'Все задачи завершены':
        'All tasks finished',
    'Журнал ящика {email}':
        'Log of mailbox {email}',
    'Остановка…':
        'Stopping…',
    'Запуск загрузки':
        'Download started',
    'В очереди':
        'Queued',
    'Пароль для {0}:':
        'Password for {0}:',
    'Пароль':
        'Password',
    'Нет ящиков для запуска.':
        'No mailboxes to run.',
    'Запуск':
        'Run',
    'Удаление почтового ящика':
        'Delete mailbox',
    'Удаление отмеченных ящиков':
        'Delete checked mailboxes',
    'Изменение почтового ящика':
        'Edit mailbox',
    'Просмотр писем':
        'View messages',
    'Экспорт почтового ящика в PST':
        'Export mailbox to PST',
    'Экспорт отмеченных ящиков в PST':
        'Export checked mailboxes to PST',
    'Остановить загрузку':
        'Stop download',
    'Запуск загрузки писем':
        'Download messages',
    'Запуск загрузки писем отмеченных ящиков':
        'Download messages of checked mailboxes',
    'Папка архива':
        'Archive folder',
    'Удалить из списка?\n\n{names}\n\nУже скачанные письма в архиве останутся на диске.':
        'Remove from the list?\n\n{names}\n\nMessages already downloaded stay on disk.',
    'Удаление':
        'Delete',
    'Добавить почтовый ящик':
        'Add mailbox',
    'Не удалось перенести письма:\n{err}\n\nНастройки ящика не изменены. Проверьте обе папки:\n{old_dir}\n{new_dir}':
        'Could not move the messages:\n{err}\n\nMailbox settings were not changed. Check both folders:\n{old_dir}\n{new_dir}',
    'Перенос писем':
        'Moving messages',
    'Перенос не удался, настройки не изменены: {err}':
        'Move failed, settings not changed: {err}',
    'Перенос не удался: {err}':
        'Move failed: {err}',
    'Перенос завершён':
        'Move complete',
    'Письма перенесены в новую папку':
        'Messages moved to the new folder',
    'Перенос писем: {old_dir} → {new_dir}':
        'Moving messages: {old_dir} → {new_dir}',
    'Перенос писем в новую папку…':
        'Moving messages to the new folder…',
    'В новой папке уже есть письма этого ящика:\n{new_dir}\n\nПеренос отменён, настройки не изменены. Выберите пустую папку или перенесите письма вручную.':
        'The new folder already contains messages of this mailbox:\n{new_dir}\n\nMove cancelled, settings not changed. Choose an empty folder or move the messages manually.',
    'Отмена':
        'Cancel',
    'Не переносить':
        "Don't move",
    'Перенести письма':
        'Move messages',
    'Если не переносить, в новой папке загрузка начнётся заново, а старые письма останутся на прежнем месте.':
        "If you don't move them, the download starts over in the new folder and the old messages stay where they are.",
    'Уже скачанные письма этого ящика лежат в:\n{old_dir}\n\nНовая папка:\n{new_dir}\n\nПеренести письма в новую папку?':
        'Messages of this mailbox already downloaded are in:\n{old_dir}\n\nNew folder:\n{new_dir}\n\nMove the messages to the new folder?',
    'Папка для писем изменилась':
        'Message folder changed',
    'Идёт загрузка этого ящика — сначала остановите её.':
        'This mailbox is downloading — stop it first.',
    'Изменение':
        'Edit',
    'Запустить выбранные':
        'Run selected',
    'Запустить отмеченные ({n})':
        'Run checked ({n})',
    'Новых писем нет (100%)':
        'No new messages (100%)',
    'Скачано новых писем: {0}':
        'New messages downloaded: {0}',
    'Архив: {0}   |   Работает: {1}   |   Отмечено ящиков: {2}':
        'Archive: {0}   |   Running: {1}   |   Checked mailboxes: {2}',
    'Журнал всех ящиков вместе':
        'Log of all mailboxes together',
    'Все':
        'All',
    'Отметить все ящики / снять все отметки':
        'Check all mailboxes / clear all checks',
    ' Одновременно ящиков: ':
        ' Mailboxes at once: ',
    'Тема: ':
        'Theme: ',
    'Папка архива…':
        'Archive folder…',
    'Открыть архив':
        'Open archive',
    'Экспорт в PST (отмеченные ящики будут выбраны заранее)':
        'Export to PST (checked mailboxes are preselected)',
    'Экспорт в PST':
        'Export to PST',
    'Просмотреть скачанные письма (выбранного ящика или всех)':
        'View downloaded messages (of the selected mailbox or all)',
    'Остановить отмеченные/выделенные (если таких нет — все)':
        'Stop checked/selected (if none — all)',
    'Остановить':
        'Stop',
    'Запустить все':
        'Run all',
    'Запустить загрузку отмеченных ящиков (или выделенного)':
        'Download checked mailboxes (or the selected one)',
    'Удалить отмеченные ящики (или выделенный, если галочек нет)':
        'Delete checked mailboxes (or the selected one if none are checked)',
    'Удалить':
        'Delete',
    'Создать ящик на основе выбранного (тот же сервер и настройки)':
        'Create a mailbox based on the selected one (same server and settings)',
    'Копировать':
        'Copy',
    'Изменить выделенный ящик':
        'Edit the selected mailbox',
    'Изменить':
        'Edit',
    'Добавить ящик':
        'Add mailbox',
    'Архивация почты: одновременная загрузка нескольких IMAP-ящиков и экспорт в Outlook PST':
        'Mail archiving: parallel download of several IMAP mailboxes and export to Outlook PST',
    '{APP_NAME} — архивация почты IMAP и экспорт в PST':
        '{APP_NAME} — IMAP mail archiving and PST export',
    'Ожидание':
        'Idle',
    'Ошибки':
        'Errors',
    'Уже в архиве':
        'Already archived',
    'Загружено':
        'Downloaded',
    'Прогресс':
        'Progress',
    'Статус':
        'Status',
    'Папки':
        'Folders',
    'Период':
        'Period',
    'Сервер':
        'Server',
    'Письмо UID {uid} в «{0}» пропущено: {e}':
        'Message UID {uid} in «{0}» skipped: {e}',
    'UID {uid}: обрыв соединения ({e})':
        'UID {uid}: connection lost ({e})',
    '«{0}»: сервер сменил UIDVALIDITY, папка будет перекачана при следующем запуске':
        '«{0}»: the server changed UIDVALIDITY, the folder will be re-downloaded next time',
    'Папка «{0}» пропущена: {e}':
        'Folder «{0}» skipped: {e}',
    'Загрузка: {0}':
        'Downloading: {0}',
    '«{0}»: писем {1}, новых {2}':
        '«{0}»: {1} messages, {2} new',
    'Анализ папок':
        'Scanning folders',
    'Папка «{0}» не найдена на сервере':
        'Folder «{0}» not found on the server',
    'Вход выполнен':
        'Logged in',
    'Подключение к {0}:{1} ({2})':
        'Connecting to {0}:{1} ({2})',
    'Подключение':
        'Connecting',
    'Ошибка: {e}':
        'Error: {e}',
    'Критическая ошибка:\n':
        'Critical error:\n',
    'Остановлено пользователем':
        'Stopped by user',
    'Готово: загружено {0}, уже было в архиве {1}':
        'Done: {0} downloaded, {1} already in the archive',
    'Завершено с ошибками: загружено {0}, пропущено из-за ошибок {1}':
        'Finished with errors: {0} downloaded, {1} skipped due to errors',
    'Обрыв соединения: {e}':
        'Connection lost: {e}',
    'Не удалось переподключиться: {e}':
        'Could not reconnect: {e}',
    'Соединение восстановлено':
        'Connection restored',
    'Переподключение (попытка {attempt}/{MAX_AUTO_RECONNECTS})…':
        'Reconnecting (attempt {attempt}/{MAX_AUTO_RECONNECTS})…',
    'Сервер отказал в выдаче письма: {data}':
        'The server refused to return the message: {data}',
    'Ошибка поиска писем: {data}':
        'Message search error: {data}',
    'Не удалось открыть папку «{0}»: {data}':
        'Could not open folder «{0}»: {data}',
    'Не удалось получить список папок: {data}':
        'Could not get the folder list: {data}',
    'Ошибка авторизации: {msg}':
        'Authentication error: {msg}',
    'Не удалось подключиться к {0}:{1} — {e}':
        'Could not connect to {0}:{1} — {e}',
    'Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт.':
        'SSL/TLS error: {e}. Check the security type and port.',
    'Сертификат сервера не прошёл проверку ({0}). Включите «Принимать все сертификаты».':
        'The server certificate failed verification ({0}). Enable «Accept all certificates».',
    'Сервер не вернул содержимое письма (письмо удалено, повреждено или недоступно)':
        'The server returned no message content (the message is deleted, damaged or unavailable)',
    '<не удалось прочитать: {e}>':
        '<could not read: {e}>',
    'Пересланное письмо':
        'Forwarded message',
    'Все папки':
        'All folders',
    'За всё время':
        'All time',
    'Без шифрования':
        'No encryption',
    'Отчёт: {report}':
        'Report: {report}',
    'Часть {i}: {0} — {1}, {2} писем, {3}':
        'Part {i}: {0} — {1}, {2} messages, {3}',
    '{0}_отчёт.txt':
        '{0}_report.txt',
    'ВНИМАНИЕ: самопроверка файла {0} не пройдена: {e}':
        'WARNING: self-check of file {0} failed: {e}',
    ', установлен пароль':
        ', password set',
    'Проверка пройдена: {0} писем, структура PST корректна':
        'Check passed: {0} messages, PST structure is valid',
    'Проверка файла {0}…':
        'Checking file {0}…',
    'Запись структуры PST…':
        'Writing PST structure…',
    'Ошибка в письме {0}: {e}':
        'Error in message {0}: {e}',
    'Папка «{0}»: {1} писем':
        'Folder «{0}»: {1} messages',
    'Часть {0} ({1}) больше лимита: в ней есть письмо крупнее заданного размера части':
        'Part {0} ({1}) exceeds the limit: it contains a message larger than the part size',
    'Часть {0} достигла лимита {1} ГБ — начата часть {n}: {2}':
        'Part {0} reached the {1} GB limit — started part {n}: {2}',
    '{0}_часть{n}{1}':
        '{0}_part{n}{1}',
    'В архиве нет писем для этого ящика — сначала выполните загрузку':
        'There are no messages in the archive for this mailbox — download it first',
    '  (писем нет)':
        '  (no messages)',
    'Писем':
        'Messages',
    'Размер файла':
        'File size',
    'Часть {i}: {0}':
        'Part {i}: {0}',
    'По':
        'To',
    'С':
        'From',
    'Папка':
        'Folder',
    'Даты указаны по дате получения письма почтовым сервером.':
        'Dates are by the date the mail server received the message.',
    'ВНИМАНИЕ: экспорт был остановлен, в файлы попали не все письма.':
        'WARNING: the export was stopped; not all messages were written to the files.',
    '{errors} (подробности — в журнале экспорта)':
        '{errors} (details are in the export log)',
    'Ошибок':
        'Errors',
    'Пароль на PST':
        'PST password',
    '{0} (размер одной части — до {gb} ГБ)':
        '{0} (part size — up to {gb} GB)',
    'Файлов PST':
        'PST files',
    'Период писем':
        'Message period',
    'Всего писем':
        'Total messages',
    'Дата экспорта':
        'Export date',
    'Почтовый ящик':
        'Mailbox',
    'нет':
        'no',
    'установлен':
        'set',
    'Отчёт об экспорте в Outlook PST — Shinra Tensei':
        'Outlook PST export report — Shinra Tensei',
    '{0:.1f} МБ':
        '{0:.1f} MB',
    '{0:.2f} ГБ':
        '{0:.2f} GB',
    'Входящие':
        'Inbox',
    'Пароль PST — только латинские буквы, цифры и знаки, без пробелов и кириллицы.':
        'PST password — Latin letters, digits and symbols only, no spaces or Cyrillic.',
    'Пароль PST — не длиннее 15 символов (ограничение Outlook).':
        'PST password — 15 characters at most (Outlook limitation).',
    'нет класса письма':
        'no message class',
    'у вложения нет данных':
        'attachment has no data',
    'родитель письма':
        'message parent',
    'счётчик писем папки «{name}»':
        'message counter of folder «{name}»',
    'нет IPM Subtree в хранилище':
        'no IPM Subtree in the store',
    'у корневой папки нет имени':
        'root folder has no name',
    'нет PR_PST_PASSWORD':
        'no PR_PST_PASSWORD',
    'счётчик NID в заголовке меньше узла {nid:#x}':
        'NID counter in the header is lower than node {nid:#x}',
    'RowID не совпадает с индексом':
        'RowID does not match the index',
    'строка таблицы обрезана':
        'table row is truncated',
    'COLDESC для RowVer должен быть ib=4, iBit=1':
        'COLDESC for RowVer must be ib=4, iBit=1',
    'COLDESC для RowID должен быть ib=0, iBit=0':
        'COLDESC for RowID must be ib=0, iBit=0',
    'ожидался Table Context':
        'Table Context expected',
    'ожидался Property Context':
        'Property Context expected',
    'BTH не отсортирован':
        'BTH is not sorted',
    'размер узла BTH':
        'BTH node size',
    'нет сигнатуры BTH':
        'no BTH signature',
    'HID {hid:#x} вне карты heap':
        'HID {hid:#x} outside the heap map',
    'подузел {hid:#x} не найден':
        'subnode {hid:#x} not found',
    'нет сигнатуры heap 0xEC':
        'no heap signature 0xEC',
    'ожидался SLBLOCK/SIBLOCK':
        'SLBLOCK/SIBLOCK expected',
    'lcbTotal не совпадает':
        'lcbTotal mismatch',
    'неполный промежуточный блок в дереве {bid:#x} (Outlook это отвергает)':
        'incomplete intermediate block in tree {bid:#x} (Outlook rejects this)',
    'ожидался XBLOCK':
        'XBLOCK expected',
    'CRC блока {bid:#x}':
        'CRC of block {bid:#x}',
    'подпись блока {bid:#x}':
        'signature of block {bid:#x}',
    'трейлер блока {bid:#x}':
        'trailer of block {bid:#x}',
    'блок {bid:#x} отсутствует в BBT':
        'block {bid:#x} is missing from the BBT',
    'AMap @{0:#x} не отмечает занятые данные':
        'AMap @{0:#x} does not mark used data',
    'нет {name} @{0:#x}':
        'no {name} @{0:#x}',
    'нет AMap @{start:#x}':
        'no AMap @{start:#x}',
    'ключи B-дерева не по возрастанию':
        'B-tree keys are not ascending',
    'подпись страницы @{ib:#x}':
        'page signature @{ib:#x}',
    'BID страницы @{ib:#x}':
        'page BID @{ib:#x}',
    'CRC страницы @{ib:#x}':
        'page CRC @{ib:#x}',
    'тип страницы @{ib:#x}':
        'page type @{ib:#x}',
    'конец файла не на границе AMap':
        'end of file is not on an AMap boundary',
    'размер файла не совпадает с ibFileEof':
        'file size does not match ibFileEof',
    'не Unicode PST':
        'not a Unicode PST',
    'CRC заголовка (full)':
        'header CRC (full)',
    'CRC заголовка (partial)':
        'header CRC (partial)',
    'нет сигнатуры !BDN':
        'no !BDN signature',
    'слишком большой объект для PST':
        'object too large for PST',
    'Удаленные':
        'Deleted Items',
    'тип свойства 0x{ptype:04X} не поддерживается':
        'property type 0x{ptype:04X} is not supported',
    'Проверьте данные':
        'Check the data',
    'Выберите папку (нажмите «Загрузить список папок»).':
        'Choose a folder (click «Load folder list»).',
    'Получение списка папок…':
        'Loading the folder list…',
    'Загружено папок: {0}.':
        'Folders loaded: {0}.',
    'Подключение успешно. Папок на сервере: {n}.':
        'Connection successful. Folders on the server: {n}.',
    'Подключение…':
        'Connecting…',
    'Нет доступа на запись в папку «{0}»: {1}':
        'No write access to folder «{0}»: {1}',
    'Укажите папку для писем или выберите «В общую папку архива».':
        'Specify a folder for messages or choose «Shared archive folder».',
    'Дата «с» больше даты «по».':
        'The «from» date is later than the «to» date.',
    'Укажите пароль.':
        'Enter the password.',
    'Укажите адрес IMAP-сервера.':
        'Enter the IMAP server address.',
    'Укажите корректный e-mail.':
        'Enter a valid e-mail.',
    'Куда сохранять письма':
        'Where to save messages',
    'Письма будут сохраняться в: {0}':
        'Messages will be saved to: {0}',
    'Укажите папку, куда сохранять письма этого ящика.':
        "Specify the folder for this mailbox's messages.",
    'Сохранить':
        'Save',
    'Проверить подключение':
        'Check connection',
    'Обзор…':
        'Browse…',
    'Выберите папку…':
        'Choose a folder…',
    'В свою папку:':
        'Own folder:',
    'В общую папку архива':
        'Shared archive folder',
    'Куда сохранять письма (.eml)':
        'Where to save messages (.eml)',
    'Загрузить список папок':
        'Load folder list',
    'Только папка:':
        'Only folder:',
    'по':
        'to',
    'с':
        'from',
    'За период:':
        'Period:',
    'Пароль:':
        'Password:',
    'Логин:':
        'Login:',
    'Протокол:':
        'Protocol:',
    'Сервер IMAP:':
        'IMAP server:',
    'Сохранить пароль (шифруется средствами Windows)':
        'Save password (encrypted by Windows)',
    'показать':
        'show',
    'обычно совпадает с e-mail':
        'usually the same as the e-mail',
    'Принимать все сертификаты (просроченные, самоподписанные, с чужим именем)':
        'Accept all certificates (expired, self-signed, wrong host name)',
    'Порт:':
        'Port:',
    'Подключение к серверу':
        'Server connection',
    'Изменить почтовый ящик':
        'Edit mailbox',
    'Экспорт ещё идёт. Остановить?\nУже записанные письма останутся в PST-файле.':
        'Export is still running. Stop it?\nMessages already written stay in the PST file.',
    'Экспорт':
        'Export',
    'Остановка: дописываю структуру текущего файла PST…':
        'Stopping: finishing the structure of the current PST file…',
    ' — есть ошибки, см. журнал':
        ' — there are errors, see the log',
    'Экспорт завершён: {0} писем':
        'Export finished: {0} messages',
    'Экспорт остановлен':
        'Export stopped',
    'ОШИБКА — {e}':
        'ERROR — {e}',
    'Готово: записано писем {0}, ошибок {err}, файлов PST: {1}':
        'Done: {0} messages written, {err} errors, PST files: {1}',
    '=== Экспорт в {pst}':
        '=== Exporting to {pst}',
    'Экспорт: {email} → {0}':
        'Export: {email} → {0}',
    'К экспорту: {0} писем в {1} ящ.':
        'To export: {0} messages in {1} mailbox(es).',
    'Подсчёт писем…':
        'Counting messages…',
    'Всего: ':
        'Total: ',
    'Экспортировано писем: ':
        'Messages exported: ',
    'Пароли не совпадают.':
        'Passwords do not match.',
    'Введите пароль для PST.':
        'Enter a PST password.',
    'Отметьте хотя бы один ящик.':
        'Check at least one mailbox.',
    'Папка для PST':
        'PST folder',
    'Для каждого ящика создаётся отдельный файл <e-mail>.pst. Файл собирается самой программой — Outlook не нужен. Если ящик больше {gb} ГБ, он делится на части (_часть2.pst, _часть3.pst…). Рядом сохраняется отчёт: какие папки и за какие даты лежат в каждой части.':
        'A separate <e-mail>.pst file is created for each mailbox. The file is built by the program itself — Outlook is not needed. If a mailbox is larger than {gb} GB, it is split into parts (_part2.pst, _part3.pst…). A report is saved next to it: which folders and which dates are in each part.',
    'Закрыть':
        'Close',
    'Экспортировать':
        'Export',
    'Какие папки и за какие даты попали в каждую часть PST':
        'Which folders and dates went into each PST part',
    'Открыть отчёт':
        'Open report',
    'Открыть папку с PST':
        'Open PST folder',
    'Все выбранные ящики:':
        'All selected mailboxes:',
    'Текущий ящик:':
        'Current mailbox:',
    'Отметьте ящики и нажмите «Экспортировать»':
        'Check mailboxes and click «Export»',
    'Ход экспорта':
        'Export progress',
    'До 15 символов: латинские буквы, цифры и знаки (ограничение Outlook). Outlook спросит пароль при открытии файла. Программа пароль не сохраняет — запомните его.':
        'Up to 15 characters: Latin letters, digits and symbols (Outlook limitation). Outlook asks for the password when opening the file. The program does not store the password — remember it.',
    'Повторите пароль':
        'Repeat password',
    'Установить пароль на PST-файл':
        'Set a password on the PST file',
    'Защита паролем':
        'Password protection',
    'От {MIN_SPLIT_GB:g} до {MAX_SPLIT_GB:g} ГБ (предел Outlook для одного файла — 50 ГБ)':
        'From {MIN_SPLIT_GB:g} to {MAX_SPLIT_GB:g} GB (Outlook limit for one file — 50 GB)',
    ' ГБ':
        ' GB',
    'Размер одной части PST:':
        'Size of one PST part:',
    'Папка для PST:':
        'PST folder:',
    'Куда сохранить':
        'Destination',
    'Снять все':
        'Clear all',
    'Отметить все':
        'Check all',
    '   (нет загруженных писем)':
        '   (no downloaded messages)',
    'Почтовые ящики из архива':
        'Mailboxes in the archive',
    'Экспорт в Outlook PST':
        'Export to Outlook PST',
    'Тёмная':
        'Dark',
    'Светлая':
        'Light',
    'Системная':
        'System',
    'Заголовки письма':
        'Message headers',
    'Нет программы для открытия .eml: {e}':
        'No program to open .eml: {e}',
    'Открыть письмо':
        'Open message',
    'Письмо (*.eml)':
        'Message (*.eml)',
    'Сохранить письмо':
        'Save message',
    'письмо':
        'message',
    'Сохранено файлов: {n}':
        'Files saved: {n}',
    'Вложения':
        'Attachments',
    'Папка для вложений':
        'Folder for attachments',
    'Сохранить вложение':
        'Save attachment',
    'Не удалось разобрать письмо: {e}':
        'Could not parse the message: {e}',
    'Вложения:':
        'Attachments:',
    'Папка:':
        'Folder:',
    'Получено:':
        'Received:',
    'Отправлено:':
        'Sent:',
    'Копия:':
        'Cc:',
    'Кому:':
        'To:',
    'От:':
        'From:',
    '(без темы)':
        '(no subject)',
    'Писем: {shown} из {total}':
        'Messages: {shown} of {total}',
    'Писем: {shown}':
        'Messages: {shown}',
    'Ошибка: {err}':
        'Error: {err}',
    'Загрузка списка писем…':
        'Loading message list…',
    'Архив пуст — сначала выполните загрузку почты':
        'The archive is empty — download mail first',
    'Открыть в почтовой программе':
        'Open in mail program',
    'Сохранить .eml…':
        'Save .eml…',
    'Заголовки':
        'Headers',
    'Сохранить вложения…':
        'Save attachments…',
    'Двойной щелчок — сохранить вложение':
        'Double-click to save the attachment',
    'отправки':
        'sent',
    'получения':
        'received',
    'Дата':
        'Date',
    'Поиск: отправитель, получатель, тема…':
        'Search: sender, recipient, subject…',
    'Обновить':
        'Refresh',
    'Ящики и папки':
        'Mailboxes and folders',
    'Просмотр архива писем — Shinra Tensei':
        'Message archive viewer — Shinra Tensei',
    'Вложений: {n}\n':
        'Attachments: {n}\n',
    '\n… и ещё {0}':
        '\n… and {0} more',
    'Когда письмо получил почтовый сервер':
        'When the mail server received the message',
    'Дата из письма (когда отправлено)':
        'Date from the message (when it was sent)',
    'Письма с вложениями (щёлкните, чтобы отсортировать)':
        'Messages with attachments (click to sort)',
    'Б':
        'B',
    'ГБ':
        'GB',
    'МБ':
        'MB',
    'КБ':
        'KB',
    'Размер':
        'Size',
    'Получено':
        'Received',
    'Отправлено':
        'Sent',
    'Тема':
        'Subject',
    'Кому':
        'To',
    'От':
        'From',
    'данные обрезаны':
        'data is truncated',
    'узел {nid:#x} отсутствует в NBT':
        'node {nid:#x} is missing from the NBT',
    'строка не в UTF-16':
        'string is not valid UTF-16',
    'повреждённая структура файла':
        'damaged file structure',
    'не удалось разобрать письмо (внутренняя ошибка)':
        'could not parse the message (internal error)',
    'внутренняя ошибка при записи письма':
        'internal error while writing the message',
}
