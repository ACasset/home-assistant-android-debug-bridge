# Android Debug Bridge USB for Home Assistant

A custom Home Assistant app (formerly called an add-on) for sending shell commands
to a USB-connected Android phone. Uses `adb-shell[usb]` directly; no ADB server or
network debugging is required.

## Installation

Requires Home Assistant OS or another installation with Supervisor and the app
store, on amd64 or aarch64. Home Assistant Container/Core alone cannot install apps.

1. Open **Settings → Apps → Install app** (on older versions,
   **Settings → Add-ons → Add-on Store**).
2. In the three-dot menu, select **Repositories** and add:
   `https://github.com/ACasset/home-assistant-android-debug-bridge`
3. Find **Android Debug Bridge USB**, install it, and start it. The image is built
   locally, so the first installation needs internet access and may take a while.
4. Open the app's web UI.

This is a custom repository, not a listing in the official default app repository.

## Phone setup

1. Enable Developer options on Android (usually by tapping **Build number** seven
   times), then enable **USB debugging**.
2. Connect the phone with a USB **data** cable to the Home Assistant host.
   If Home Assistant runs in a VM, pass the USB device through to that VM first.
3. Open this app through Home Assistant, refresh the device list, select the phone,
   and run `getprop ro.product.model`.
4. Unlock the phone and accept **Allow USB debugging?**. Choose **Always allow
   from this computer** if desired. If authorization times out, approve and retry.

The app keeps its ADB identity in `/data/adbkey` and `/data/adbkey.pub`, so restarting
does not require a new authorization. Treat app backups as sensitive: they contain
the private key. To revoke access, use **Revoke USB debugging authorizations** in
Android's developer settings.

## Commands

Enter Android shell commands, without the `adb shell` prefix. Examples:

```sh
getprop ro.product.model
input keyevent KEYCODE_HOME
input text Hello
dumpsys battery
```

Multiple phones can be listed; each request targets the selected serial. Missing
or duplicate serials are rejected rather than sending commands to an arbitrary
device. Operations are serialized because the USB transport is not thread-safe.
Connections are closed after each command, so unplugged phones can be reconnected
and selected again after refreshing.

Commands have a 60-second timeout and responses are limited to 1 MiB of output
(the library buffers output before this check). This is a text shell interface,
not the desktop `adb` CLI: `adb install`, `adb push`, file transfers,
interactive terminals, and binary output are not supported. Output is displayed
as text; a successful request means the shell stream completed, not necessarily
that the Android command returned exit status zero.

## Security and troubleshooting

Only run trusted commands: Android shell access can modify or delete phone data.
The UI is accessed through Home Assistant ingress with an administrator-only panel.
No host port is published, and direct requests outside the ingress proxy are
rejected. Keep Home Assistant administrator access secure. There is no public
REST endpoint or Home Assistant service for automations.

If no device appears, check the cable, USB debugging, VM passthrough, and whether
another ADB process/app has claimed the USB interface. Stop competing ADB tools.
Raw USB access is enabled by the app configuration; disabling Home Assistant
protection mode or granting full host access should not be necessary.

Connection errors appear in the UI; details are available in the app logs.
USB support in `adb-shell` is experimental and may vary by phone and host.

## Development

The app uses Python's standard-library HTTP server, Home Assistant ingress, and
`adb-shell[usb]`. Install `android_debug_bridge/requirements.txt` in a virtual
environment, then run `python -m unittest discover -s tests -v`.

Build the container from the repository root with:
`docker build -t android-debug-bridge-usb android_debug_bridge`.
Real USB authorization must be verified with a connected Android device on the
Home Assistant host.