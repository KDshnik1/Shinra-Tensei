# Changelog — Shinra Tensei

> Русская версия: [CHANGELOG.ru.md](CHANGELOG.ru.md)

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers are informal; the project was started on 2026-10-15.

---

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
