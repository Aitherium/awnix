# Install Aither on Windows

This page is for anyone, including people who have never used a command window.
You need a Windows 10 or Windows 11 laptop or PC, an internet connection, and about
15 minutes. You can keep using the computer while it installs.

## The one thing to do

1. Press the **Windows key** and the **R** key at the same time. A small box called
   **Run** opens.

   > [screenshot: the Run box]

2. Copy this line, paste it into the box, and press **Enter**:

   ```
   powershell -ep Bypass -c "irm https://raw.githubusercontent.com/Aitherium/awnix/main/.DEPLOYMENT/standalone/bootc/install-aither-windows.ps1|iex"
   ```

   > [screenshot: the line pasted into the Run box]

That is all. A blue window opens and tells you what it is doing, with a percentage.

Prefer a file you can double-click? Download **Install-Aither.cmd** from the same place
and double-click it. Windows may show **"Windows protected your PC"**, because the file
is not signed. Click **More info**, then **Run anyway**.

> [screenshot: "Windows protected your PC" with "More info" and "Run anyway"]

## What you will see

**Step 1 of 5: Checking this computer.** This takes a few seconds. Your web browser then
opens the Aitherium page. **You can start making things there straight away**, for example
your first AI video. You do not have to wait for the rest of the install.

> [screenshot: the Aitherium page in the browser]

**Step 2 of 5: Turning on Windows Subsystem for Linux.** Most computers need this only
once. Windows asks **"Do you want to allow this app to make changes to your device?"**
Click **Yes**. The installer then asks to restart the computer **one time**:

- Save anything you have open.
- Type **Y** and press **Enter** to restart now, or restart later yourself.
- After the restart, sign in as usual. **The blue window comes back by itself** and
  carries on.

> [screenshot: the restart question]

If you are sharing your screen, the restart ends the call. Join again after you sign in.

**Step 3 of 5: Downloading Aither.** This is the long part: about 1.7 GB. The window shows
the percentage and how many minutes are left. If the internet drops, the window says so
and keeps trying. Nothing that already downloaded is lost.

> [screenshot: download progress with percentage]

**Step 4 of 5: Installing Aither.** A few minutes. The bar can stay still for a while.
That is normal.

**Step 5 of 5: Connecting to your Aitherium account.** The browser opens a sign-in page,
and the blue window shows a short code. Check that the page shows **the same code**, then
click **Approve**.

> [screenshot: the code in the window, and the same code on the Approve page]

When it is finished, the window says **"Aither is installed and connected"** and lists how
long each step took on your computer. Press **Enter** to close it.

## How long it takes

These are estimates for the download size measured on 2026-11-11. The download times are
plain arithmetic. The other rows have **not** been timed on a real laptop yet; the window
prints the real times when it finishes.

| step | 25 Mbps home internet | 100 Mbps home internet |
|---|---|---|
| 1. checking + browser opens | under 10 seconds | under 10 seconds |
| 2. turning on WSL + one restart (only if needed) | about 4 to 7 minutes | about 4 to 7 minutes |
| 3. download (about 1.7 GB) | about 9 minutes | about 2 to 3 minutes |
| 4. installing | about 1 to 3 minutes | about 1 to 3 minutes |
| 5. connecting (one click) | under 1 minute | under 1 minute |
| **total, WSL already on** | **about 11 to 13 minutes** | **about 4 to 6 minutes** |
| **total, WSL needs turning on** | **about 15 to 20 minutes** | **about 8 to 12 minutes** |

You can start creating on the Aitherium page about **one minute** after pressing
Enter, whatever the speed of your internet. The work runs on the platform, not on this
computer.

## If something goes wrong

The window always says what to do next in one sentence. The common ones:

| the window says | do this |
|---|---|
| Virtualization is turned off | Restart, open the BIOS/UEFI setup (often F2, F10 or Del at start-up), turn on "Virtualization" (Intel VT-x or AMD SVM), then run the same line again. Ask someone to help if this sounds hard. |
| Windows has not finished turning on WSL | Restart the computer once, then run the same line again. |
| The disk is full | Free at least 12 GB, then run the same line again. |
| The internet connection kept dropping | When the internet works again, run the same line again. The download continues where it stopped. |
| Something unexpected happened | Take a screenshot of the window and send it to the person helping you. |

Running the same line again is always safe. It skips what is already done.

## For helpers: what the line does

- It runs `install-aither-windows.ps1` from the public `Aitherium/awnix` repository. Read
  it first if you like; `-DryRun` shows every action without changing anything.
- It downloads the newest `awnix-wsl-base-<date>` release. That release holds a prebuilt
  awnix root filesystem in parts, a manifest (`awnix-wsl.json`) and `SHA256SUMS`. Every
  part and the joined file are checked against their sha256.
- It imports it as the WSL distro `awnix`, turns on systemd, and links the box with
  `awnix-setup --link`. That is the awnix device-code sign-in; the token stays inside the
  distro in `/etc/awnix/link.token`.
- Options: `-Edition full` or `-Edition ai-full` for the bigger images, `-NoBrowser`,
  `-DryRun`.
- Files it keeps: `%LOCALAPPDATA%\Aither` (`install-times.json` holds the measured times).
- To remove Aither, open PowerShell and run `wsl --unregister awnix`. This deletes the
  distro and everything in it.
