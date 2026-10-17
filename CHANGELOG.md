# Changelog — Shinra Tensei

> Русская версия: [CHANGELOG.ru.md](CHANGELOG.ru.md)

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are informal; the project was started on 2026-10-15.

---

## [0.17.0] — 2026-10-17

### Fixed
- **The main window no longer falls apart when it is resized.** Before, only the Status column
  stretched: after making the window narrower, E-mail, Server, Period and Folders stayed squeezed to
  stubs even when the window was maximized again, and Errors went off the right edge. Now the columns
  share the width of the window (E-mail, Server and Status get most of the free space), never get
  narrower than their header, and come back when the window is widened. Only when the window is
  narrower than all the columns together does a horizontal scroll bar appear.
- The theme, the language and "Mailboxes at once" were cut off on the right of the toolbar on
  windows narrower than about 1,400 px. They moved to the new **Settings** button on the toolbar.
  Toolbar buttons that do not fit go to the » menu at its right end (that button was invisible in
  the themes before).
- The export window: on a narrow window the hint lines under "Size of one PST part" overlapped the
  other settings, and the window did not fit a 768 px tall screen. The settings now scroll, while
  the export progress, log and buttons stay visible; the mailbox list is as tall as the number of
  mailboxes.
- The message viewer: the same column layout as the main window; the folder tree can no longer be
  squeezed to nothing by the splitter.

### Changed
- The window size and position, the log height and the column widths you set with the mouse are
  remembered between starts. Settings → "Default column widths" (or right-click on the table header)
  resets the columns.
- The subtitle and the status bar end with "…" when they do not fit; the full text is in the
  tooltip. The main window cannot be made smaller than 760 × 480.
- During a download the mailbox table is redrawn at most about 7 times a second instead of on every
  progress signal, so the window stays responsive when several mailboxes download quickly.

## [0.16.0] — 2026-10-17

### Added
- **"Don't let Windows sleep during download and export"** checkbox in the status bar of the main
  window (on by default). While a download or a PST export runs, the computer does not go to sleep
  when idle; the screen may still turn off. Closing a laptop lid or choosing Sleep in the Start menu
  still puts it to sleep. The request shows up in `powercfg /requests` as "Shinra Tensei" and is
  removed as soon as the last task ends.
- App icon and file version: the exe has its own icon in Explorer and the taskbar, and
  Properties → Details shows the version.

### Changed
- **The app is now a folder instead of a single exe**, and starts several seconds faster. The single
  exe unpacked Python and Qt to `%TEMP%` on every start and the antivirus scanned every DLL again.
  Releases are now `Shinra-Tensei-<version>-win64.zip`: unzip the folder and run
  `Shinra Tensei.exe`. Settings and archives are not affected.
- While a download or an export runs, Windows 11 no longer slows the app down (EcoQoS) when its
  window is minimized. When nothing runs, Windows decides as usual.
- The build is smaller: parts of Qt the app does not use are left out (software OpenGL, the
  on-screen keyboard with Qt Quick and QML, Qt networking, PDF and rare image formats, Qt
  translations other than Russian and English). Build settings moved to `shinra.spec`.
- The exe is no longer packed with UPX, which often caused false antivirus alarms, and its Python
  code is compiled with `optimize=2`.
- CI starts the built exe with `--smoke-test` and fails if the window does not open, the native core
  or the Qt translation is missing, or the icon is not found.

## [0.15.0] — 2026-10-17

### Added
- **Mail download rewritten in Rust.** Connecting to the server (SSL/TLS, STARTTLS, unencrypted),
  signing in, listing folders, preparation, the batch queue, connections, reconnects, duplicate
  detection and writing messages to the archive are now done by the Rust core. The app's windows
  stay in Python. The archive format is unchanged, so existing archives resume as before.
  - Stop takes effect at once: the core closes the connections instead of waiting for the server
    to answer. In a test with a server that replies every 8 seconds, stopping took 0.1 s instead of
    39 s.
  - Messages in a batch are saved as they arrive, not after the whole batch. When a connection
    drops, only the messages that did not arrive are requested again.
  - The mailbox window uses the core too: "Check connection" and "Load folder list".
  - If the core cannot agree on encryption with the server (old servers that only know TLS 1.0/1.1),
    the download switches to the Python module by itself and says so in the log. Without the core,
    or with `SHINRA_PURE_PYTHON=1`, everything runs on Python as before.
- **Two-step mailbox window.** "Save" is replaced by "Next", which opens a "How to download mail"
  page. It has two choices, and each option says what it does, with its pros and cons:
  - server connections: one or several (2–10);
  - duplicates: "Automatic (recommended)", "Always look for duplicates" or "Don't look, download
    everything as is".
- Connection tests: SSL/TLS and STARTTLS with a self-signed certificate, rejecting an untrusted
  certificate, signing in with a quote in the login and a non-ASCII password, falling back to the
  Python module. Tests for the mailbox window.

### Changed
- **Preparation is shorter and no longer holds up the download.** The app used to ask the server
  for the sizes and Message-IDs of all new messages in all folders before downloading anything, so
  large mailboxes sat at "Preparing: INBOX" with 0 messages for a long time. Now a folder is
  prepared in parts of 1,000 messages, and each part's batches start downloading straight away. The
  status shows progress: "Preparing: INBOX (3,000 of 20,978)".
- **Duplicates are only looked for where they occur.** In "Automatic" mode Message-IDs are requested
  only for mailboxes with an "All Mail" folder (Gmail). On mail.ru, Yandex, Exchange and other
  servers a message lives in one folder, and preparation only asks for sizes. Mailboxes that had
  duplicate skipping turned off get "Don't look".

### Fixed
- A login containing a space or a quote failed: the Python module sent it unquoted.

## [0.14.0] — 2026-10-17

### Added
- **Several connections per mailbox.** The mailbox settings have a new "Connections" field (1 to
  10, default 1, as before). Message batches are handed to the connections from a shared queue, so
  a single large folder gets faster too, not only many folders. On a test server that serves
  4 MB/s per connection, 1,600 messages (48 MB) took 16 s with one connection, 9 s with two, 5.7 s
  with four and 4.1 s with eight.
  - No extra connections are opened: never more than there are batches.
  - If the server does not allow that many connections, the download goes on with the ones that
    opened. If a connection drops and cannot reconnect, the other connections finish its batch;
    messages already downloaded are not requested again.
  - Duplicate skipping works the same: each message is downloaded once, and Gmail's "All Mail" waits
    until the regular folders are done, so messages stay in their label folders.
  - "Stop mailbox" in the error dialog and the Stop button stop all of the mailbox's connections.
- The download tests also run with three connections. New tests: the server's connection limit,
  dropped connections, stopping and resuming, and duplicates across connections.

## [0.13.0] — 2026-10-17

### Added
- **Duplicate messages are not downloaded.** In Gmail the same message sits both in its label
  folder (such as Inbox) and in "All Mail", and used to be downloaded twice. Before downloading a
  folder, the app now asks the server for the Message-IDs and sizes (without the messages) and skips
  those already in the mailbox archive or downloaded earlier in this run. "All Mail" goes last, so
  messages stay in their label folders and "All Mail" gets only messages without labels. Messages
  without a Message-ID are always downloaded. The "Don't download duplicates of messages from other
  folders" checkbox in the mailbox settings is on by default.
- **Duplicates are not written to the PST.** A checkbox in the export window (on by default) drops
  copies of the same message from different folders: the first copy stays, and "All Mail" goes last.
  The number of skipped duplicates goes to the log and the report.
- **Viewer filters:**
  - where to search: everywhere, or only sender, recipients, subject or attachment names
    ("everywhere" now also searches attachment names);
  - "Only with attachments";
  - "Size from … MB";
  - a "Reset filters" button.
  The date filters moved to a second row next to the new ones.
- **Automated tests** in `tests/` (`python -m unittest discover -s tests -t .`), 34 tests, about
  25 s:
  - message parsing: Rust and Python give the same result, and 300 corrupted messages and thousands
    of nested parts crash neither version;
  - PST: Rust and Python write byte-identical files, exporting a mailbox with either version gives
    the same messages, and both self-checks answer the same on 150 corrupted files;
  - downloading from a fake IMAP server: batches, large messages, dropped connections, missing
    messages, servers without batch fetch, sizes or header fetch, unsolicited responses,
    duplicates;
  - the viewer index, text search and the new filters.
  GitHub Actions runs them twice on every push: with the native core and with
  `SHINRA_PURE_PYTHON=1`. If they fail, the exe is not built.
- **GitHub releases.** A version tag (`git tag v0.13.0 && git push origin v0.13.0`) builds the exe
  and publishes it under Releases, with that version's changelog section as the description.

### Fixed
- After a dropped connection, the old connection's socket stayed open until garbage collection:
  imaplib does not close it when LOGOUT fails. It is now closed right away.
- README.md listed `models.py` under `native/` instead of `core/`.

## [0.12.0] — 2026-10-17

### Added
- **Search in message text.** An "in message text" checkbox next to the search box also searches
  the message itself (plain text, or HTML without markup). The first search indexes the folder
  (21,000 messages take about 6 s with the native core, with progress in the status line); after
  that the index opens in about 1 s. Texts are stored in `text_index.gz` in the message folder, and
  only new and changed messages are parsed again.

### Changed
- **Mail is downloaded in batches.** Each message used to be requested with its own command and a
  full round trip. Messages now come in batches of up to 100 messages or 16 MB; large ones still go
  one at a time. On a test server with 30 ms of latency, 1,657 messages took 2.7 s instead of
  113 s. Messages missing from a server response are downloaded one by one with the usual retries
  and "skip?" prompt. A dropped connection re-requests the batch, and a server that does not accept
  such requests is switched to one at a time automatically.
- **PST export is 1.6× faster** (3,000 messages: 2.7–3.6 s → 1.6–2.3 s). The native core parses
  messages on several threads and writes them in order. The PST contents are the same.
- Opening a mailbox with many folders for the first time is faster: new messages of all folders
  are summarized by one thread pool (60 folders: 0.71 s → 0.50 s). Selecting another folder before
  a load finishes now stops the old load instead of slowing down the new one.
- `native/check.py` now says that its Rust PST write time includes converting data from Python. The
  export does not do that; there Rust writes about 5× faster than Python.

## [0.11.0] — 2026-10-17

### Changed
- **The message viewer is faster.** On a folder of 21,000 messages:
  - sorting by a column takes 0.15 s instead of 9–17 s: rows are sorted in the model itself instead
    of through `QSortFilterProxyModel`, which called Python code on every comparison;
  - the first time a folder is opened (building the index) takes 3 s instead of 46 s: the native core
    parses the headers on several threads;
  - opening it again takes 1.5 s instead of 18 s, and the window no longer freezes: rows are no
    longer sorted through the proxy model, files are listed together with their sizes in one pass,
    and table dates are prepared on a background thread;
  - search runs 0.2 s after the last typed letter instead of on every letter.
- Without the native core the viewer uses the Python code as before (sorting and search are faster
  there too).

## [0.10.1] — 2026-10-16

### Fixed
- **Message received dates.** In the PST and the export report every message showed the day the
  archive was downloaded instead of the day it was received: the date came only from the message
  file's timestamp, which stays at the download time when the server sends INTERNALDATE in a form
  `imaplib` does not understand. The received date now comes from the top Received header (added by
  the mail server on delivery), from Date for sent messages and drafts, and from the file timestamp
  only when neither exists. There is no need to download the archive again; just export it again.
  The viewer rebuilds its index and shows the right dates too.
- INTERNALDATE is parsed more leniently (single-digit day, any letter case in the month), and when
  the server sends none, the file timestamp is taken from the message headers.
- A message with thousands of nested parts (multiparts or forwarded messages inside each other)
  crashed the whole app when exporting with the native core: the stack overflowed. Parts nested
  deeper than 100 levels are no longer parsed and the rest is kept as is. The Python code failed on
  such a message and skipped it; now it is exported with its headers.
- The self-check of a damaged PST could recurse forever (a cycle in the block or folder tree), and
  the Python code crashed with `IndexError`, `struct.error` and so on, aborting the export after the
  files were written. Any damage now gives a normal self-check message ("damaged file structure",
  "data is truncated"…). Both versions were tested on 7,500 corrupted files and give the same
  verdicts.
- If the next PST part could not be created after the previous one was closed (for example, the
  disk was full), the Python code deleted the finished part.
- Message text without a declared charset in Windows-1251 or KOI8-R turned into "�����". The
  encoding is now detected the same way as for headers: UTF-8, Windows-1251, KOI8-R.
- Messages with an 8-bit Message-ID, a null byte in the charset name or broken Content-Type
  parameters (which crash Python's header parser) were left out of the PST. The Python code now
  parses messages in compat32 mode, which is sturdier and almost twice as fast (the test mailbox
  exports in 13 s instead of 24 s).
- A subject or name with broken base64 in an encoded word (`=?utf-8?B?…***?=`) turned into
  gibberish. Such a header is now shown as is.
- An attachment name with a path (`C:\dir\file.txt`) went into the PST whole; now only the file
  name does.
- Attachment names in raw Windows-1251 are now read correctly by the Python code too.
- Strings with invalid characters (lone UTF-16 surrogates) no longer stop the PST from being
  written; such characters are replaced with "?".
- An internal error in the native core no longer aborts the export: the message counts as an error
  and the export goes on.
- The Python self-check no longer builds the text of every possible error message in advance
  (millions of needless translation calls on a large file).

## [0.10.0] — 2026-10-16

### Added
- **Native core in Rust** (`native/`, optional). PST export parses `.eml` files, writes the PST and
  runs the self-check in Rust. A 3,000-message test mailbox (200 MB) exports in 1.9 s instead of
  25 s. The heavy work runs without the GIL, so two exports at once now give 1.7× the throughput
  instead of slowing each other down.
- The PST files are byte-for-byte identical to the Python writer's. The self-check gives the same
  verdicts, including on deliberately corrupted files.
- `native/check.py` compares both versions on a real mailbox.
- `SHINRA_PURE_PYTHON=1` forces the pure-Python code.

### Changed
- Forwarded messages (`message/rfc822` attachments) are exported with their original bytes. The
  Python code rebuilt them, which refolded headers.
- Attachment names written in raw Windows-1251 without encoding are now read correctly
  ("Счёт.txt" instead of "����.txt").

## [0.9.0] — 2026-10-16

### Added
- **English interface.** Switch in the toolbar: "Язык / Language" → Русский / English. The change is
  applied after a restart, which the app offers to do right away (unless a download or export is
  running). On first start the language follows Windows.
- In English mode the whole app is translated: windows, menus, tooltips, statuses, logs, error
  messages, PST self-check messages and the export report (`<e-mail>_report.txt`, parts named
  `_part2.pst`…). PST folders are named "Inbox" and "Deleted Items".
- Language-specific formats: dates `2026-10-16` / `16.10.2026`, numbers `1,234` / `1 234`,
  decimals `2.5` / `2,5`.
- In Russian mode Qt's own Russian translation is loaded when available, so standard dialog buttons
  ("Да", "Нет", "Отмена") are in Russian too.

### Changed
- All interface texts are now looked up through `tr()`; translations live in `core/lang_en.py`.
- The export report aligns its columns for any language.

---

## [0.8.0] — 2026-10-16

### Added
- **Per-mailbox storage folder for `.eml` files.** The mailbox dialog has a new
  "Where to save messages" section: the shared archive folder or a custom folder ("Browse…").
  The resulting path is shown below, e.g. `D:\Mail\Ivanov\ivanov@mail.ru`.
- Write-access check for the chosen folder when the mailbox settings are saved.
- **Moving already downloaded mail** when the folder changes: "Move messages" / "Don't move" /
  "Cancel". On the same drive the move is instant; while it runs, the mailbox is locked and its
  status is shown in the table.
- The move is cancelled, with nothing changed, if the new folder already contains mail of this
  mailbox.

### Changed
- The per-mailbox folder is used everywhere: download, "Open archive", message viewer and PST
  export.
- The viewer lists mailboxes from both the shared archive folder and their own folders.

### Fixed
- Changing a mailbox's e-mail address no longer loses the link to its downloaded mail: the app
  offers to move it.

---

## [0.7.0] — 2026-10-16

### Added
- **Independent (non-modal) export window.** While an export runs, the main window, mail
  downloads and the viewer keep working. Several exports can run at once. The window has its own
  taskbar button and can be minimized.
- **Tabbed log:** an "All" tab plus one tab per mailbox. Export progress is also written to the
  mailbox tab, marked `[PST]`. Selecting a mailbox in the table switches to its tab. The tab of a
  mailbox that finished with an error is highlighted.
- **Export report** `<e-mail>_отчёт.txt` saved next to the PST, plus an "Open report" button.
  For every part: file name, size, message count, overall period, and each folder with its
  message count and from–to dates (by received date).
- **Configurable PST part size:** 0.5–50 GB, default 10 GB; the value is remembered.
- Read support for compressed `.eml.gz` messages throughout the app (compression itself is not
  enabled yet).

### Changed
- Messages in each folder are written in received-date order, so each part covers a continuous
  period with no overlapping dates between parts.
- Accurate splitting: the app estimates the size of internal structures and of the next message
  in advance, so each part stays within the limit. Only a part holding a single message larger
  than the limit can exceed it; this is noted in the log.
- When the export window or the app is closed mid-export, the current PST file is finalized
  cleanly, so it is not left corrupted.
- Numbers in the export log use thousands separators (`64 000 писем`).
- Viewer index upgraded to a new version; an index from the previous version is reused without
  re-parsing messages.

### Fixed
- Export order: files used to be sorted as strings, so messages inside a folder came out of
  order.

---

## [0.6.1] — 2026-10-15

### Added
- **Paperclip icon** for messages with attachments in the viewer. The app draws it in the theme's
  text colour, so it stays visible in dark and light themes and on the selected row. With several
  attachments their number is shown next to it; hovering shows the file names. The icon is also
  in the column header (sorting) and in the message's attachment list.
- "Attachments: N" line in the message card.

### Fixed
- The paperclip appeared on messages without attachments, e.g. newsletters with embedded logos.
  The message list and the preview now count attachments the same way.
- A forwarded message is shown as one `<Subject>.eml` attachment instead of loose parts.

---

## [0.6.0] — 2026-10-15

### Added
- **Percentages in download progress:** `12 345 / 90 000 (13%)`; when there is nothing new —
  "Новых писем нет (100%)".
- **Export progress:** bars for the current mailbox and for all selected mailboxes, with message
  counts and percentages; "Writing PST structure…" and "Checking file…" stage labels.
- **Checkboxes for selecting several mailboxes.** Clicking the ✔ column header ticks or clears
  all. Run, stop, delete and export act on the ticked mailboxes, or on the selected one if none
  are ticked.
- "Select all" / "Clear all" buttons in the export window; mailboxes ticked in the table are
  preselected.
- **Right-click context menu:** start download, export to PST, view messages, edit mailbox,
  delete mailbox, plus "Stop download" while a download runs. Right-clicking one of several ticked
  mailboxes applies the action to all ticked ones.

### Changed
- **Maximum PST file size raised from ~1.9 GB to 10 GB.** Added the FMap and FPMap service pages
  needed for files over 2 GB; their placement was cross-checked against Microsoft's official
  `outlook-pst-rs` library.
- PST self-check now verifies FMap/FPMap placement and checksums.
- Lower memory use when exporting large mailboxes: about 120 MB for a 2.4 GB file.

---

## [0.5.0] — 2026-10-15

### Changed
- **Renamed to Shinra Tensei** (formerly MailArchiver). The executable is `Shinra Tensei.exe`.
  Settings and logs moved to `%APPDATA%\Shinra Tensei\`.

### Added
- Automatic migration of settings from the old name on first start.
- English README (`README.md`) and Russian README (`README.ru.md`).
- MIT license (`LICENSE`) and a section on third-party components and their licenses.

---

## [0.4.1] — 2026-10-15

### Fixed
- **Outlook refused to open generated PST files** ("Server unavailable…"). Based on the SCANPST
  report:
  - every block in a data chain except the last one is now completely filled, as Outlook requires;
  - fixed the positions of the RowID and RowVer columns in tables;
  - table column sets now match the ones Outlook requires;
  - node ID high-water marks in the header now account for all objects, including attachments;
  - the store password property is always present (0 = no password); the root folder has a name.

### Changed
- Stricter PST self-check: it now tests the same rules Outlook tripped over.

---

## [0.4.0] — 2026-10-15

### Added
- **Built-in PST writer** implemented from Microsoft's open [MS-PST] specification. Outlook is no
  longer needed for export — neither running nor installed.
- Independent PST reader for **self-verification of every file** after export: checksums,
  signatures, B-trees, tables and per-folder message counts.
- PST password without Redemption: up to 15 characters, ASCII letters, digits and symbols.
- Automatic splitting of large mailboxes into parts (`_часть2.pst`, …), at ~1.9 GB at that time.
- Exported fields: subject, sender, To/Cc recipients, sent and received dates, plain text and
  HTML bodies, attachments of any size, original headers, read/unread state.

### Removed
- Export via Outlook (COM) and Redemption, together with the Outlook dependency.

### Fixed
- "The object could not be found" errors during export, which came from automating Outlook.

---

## [0.3.0] — 2026-10-15

### Added
- **Built-in viewer for downloaded mail:** mailbox/folder tree, message list with sent and received
  dates, sorting, search, date filter, HTML/text preview with inline images (remote images are
  never loaded), saving attachments and `.eml`, raw headers, opening in the default mail client.
- PST password fields in the export window (via Redemption) and an export engine indicator.

### Fixed
- Export error "Invalid path or URL" (Outlook would not accept `.eml` files).

---

## [0.2.0] — 2026-10-15

### Added
- **Themes: System, Light, Dark** (toolbar switch). "System" follows the Windows theme, including
  live changes.

### Fixed
- Invisible text in dialogs when Windows used a dark theme (white text on white background).
  Palette and styles are now defined together for each theme.

---

## [0.1.1] — 2026-10-15

### Fixed
- `build.bat` failed with commands split into fragments. The file is now saved with Windows line
  endings and ASCII only; it locates Python, checks every step and reports a clear error instead of
  a false "Done".

---

## [0.1.0] — 2026-10-15

First release (under the name MailArchiver).

### Added
- Parallel download of several mailboxes over IMAP; the number of simultaneous downloads is
  configurable.
- Mailbox settings: e-mail, server, port, security (SSL/TLS, STARTTLS, none), "Accept all
  certificates" (expired, self-signed, legacy TLS), login, password, "Check connection" button.
- Download period: all time or a from/to date range.
- All folders or a single selected folder (folder list loaded from the server; non-Latin folder
  names supported).
- Per-message error dialog: Skip / Skip all errors for this mailbox / Retry / Stop; automatic
  reconnect on dropped connections.
- Incremental download: re-running fetches only new messages.
- Messages stored as `.eml`, keeping the received date and flags.
- Passwords stored encrypted with Windows DPAPI.
- PST export via Outlook (later replaced, see 0.4.0).
- MailStore-inspired interface; log in the window and in log files.
- Build to `.exe`: `build.bat` and GitHub Actions.
