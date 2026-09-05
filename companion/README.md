# Empire Earth Companion (Windows)

This companion supports the **original Empire Earth**, including the window title used by NeoEE. It creates a reviewed draft from the post-game Military screen and submits it to the Empire Earth MMR ladder for administrator approval.

## Use it

1. Run `Empire-Earth-Companion-0.1.1.exe` on Windows 10/11 x64. This is a portable, unsigned build; no administrator rights are required.
2. Ask your ladder administrator to create a device token on the ladder’s **Companion** page. Enter `https://empire-mmr.duckdns.org` and that token, then click **Connect ladder**.
3. Click **Watch for game** before playing. At the end, open **Military** and keep **Units Killed** and **Units Lost** visible. The companion watches only the identified game window, runs English OCR locally, and stops when it finds a draft. Capture polling waits eight seconds after each OCR pass.
4. Alternatively, choose **Capture now**, **Import image**, or **Add player** to enter the match manually.
5. Map every row to a ladder player. Correct all kills/losses, assign both teams, and choose the winner. Check the confirmation, click **Preview MMR**, then **Submit for approval**.

All submissions start in the approval queue. Approval is required before the match changes ratings. Previewed values can change if other matches are approved first. The companion never determines the winner from military numbers, and never submits automatically.

## Capture and OCR limits

- The game must have a visible window titled **Empire Earth**; sequel, browser, and companion windows are excluded. If multiple matching game windows are open, close the extra ones.
- Legacy exclusive fullscreen or minimized DirectDraw windows may produce blank/blocked captures. Use windowed mode or import a screenshot taken by the game/Windows. The companion does not fall back to recording the entire desktop.
- The English language model is bundled in the executable. No language download, cloud OCR, screenshot upload, or game memory injection is required.
- Version 0.1.1 enlarges small text and reads kills/losses using their column positions, including the full six-column Military layout. A detected Military screen opens for review even when some rows or numbers are unreadable; missing values stay blank for manual entry.
- OCR still makes mistakes in small text, stylized names, color bars, photos, and non-English interfaces. The supplied angled phone photo can detect the Military headings but cannot reliably provide every player row. Use a native screenshot and check every number before submission.
- Only exact normalized names are mapped automatically. Unknown or misspelled names stay unassigned. Names such as `???????` require you to select the correct ladder player; the companion does not guess an identity.
- This version reads post-game images. Empire Earth save/replay parsing is a separate integration; `.ees` files are not imported as military statistics in this release.

## Local data and connection safety

Settings and drafts are stored under `%APPDATA%\empire-earth-companion`. Tokens are encrypted using Electron `safeStorage` (Windows user-account protection). A token is never returned to the renderer or included in logs. Click **Forget token** to remove it.

Screenshots are retained in the `captures` subfolder; **Open local captures** opens it. OCR text stays inside the local draft. The server receives player IDs, teams, winner, reviewed statistics, capture time, submission UUID, and (for screenshots) a SHA-256 fingerprint. No image bytes, local paths, or OCR text are sent. Manual reports have no screenshot fingerprint. Remove local captures manually when you no longer need them.

Draft edits are saved automatically. The exact submission payload and UUID are saved before sending. If a connection fails, **Retry saved submission** sends exactly that payload and UUID again. The server returns the previous receipt instead of creating the same submission twice. An unresolved submission is locked against edits; its server address cannot change until it is resolved. You can re-enter a token to recover authorization. Starting a new match archives the prior draft under `history`; unresolved submissions warn before proceeding. Do not start another report for the same game after a timeout—retry first.

The server must use HTTPS. Plain HTTP is accepted only for literal localhost development. Redirects are rejected so the bearer token cannot be forwarded to another host. A different server requires a fresh token. The renderer runs sandboxed with context isolation, no Node integration, a strict content security policy, a narrow IPC bridge, denied navigation/popups, and no direct network access.

## Development and packaging

```powershell
cd companion
npm ci
npm test
npm start
npm run build
```

`npm run dist` is an alias for `npm run build`. The portable executable is written to:

```text
companion/dist/Empire-Earth-Companion-0.1.1.exe
```

Electron and its build dependencies are pinned in `package-lock.json`. The English OCR model and worker/WASM files are included in the package; only the first development/package setup requires network access.

`npm run smoke` uses an isolated temporary profile, verifies the local bridge/renderer, and writes ignored `smoke-result.json` and `smoke-ui.png` files. `npm run smoke -- --smoke-capture` additionally checks capture against the companion’s own test window, temporarily retitled Empire Earth; it does not prove legacy DirectDraw capture support. With the real game already visible, `npm run smoke -- --smoke-watch-game` runs the actual watch/capture/OCR/draft flow in a hidden window and reports the recognized numbers. It never submits a match or uses the normal profile's credentials. To diagnose a local PNG without uploading/copying it:

```powershell
npm run check:ocr -- "C:\path\to\military.png"
```

Tests cover OCR row/coordinate parsing, original-game window recognition, explicit winner confirmation, stat bounds, duplicate roster prevention, payload privacy, pre-send persistence, retry identity, server-origin restrictions, redirect refusal, and API failure handling.

Implementation references: [Electron desktop capture](https://www.electronjs.org/docs/latest/api/desktop-capturer), [safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage), [Tesseract.js API](https://github.com/naptha/tesseract.js/blob/master/docs/api.md), and [electron-builder Windows packaging](https://www.electron.build/win/).
