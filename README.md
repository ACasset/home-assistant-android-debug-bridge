# Android Debug Bridge USB for Home Assistant

A HACS-compatible **custom integration** for discovering USB-connected Android
phones and sending Android shell commands from Home Assistant actions and
automations. Uses `adb-shell[usb]` directly; no ADB server or network debugging.
Includes an experimental, coordinate-based SMS action for internet-outage alerts.

## Installation

Requires Home Assistant 2025.4.4 or later and USB access from **Home Assistant Core**.

1. In HACS, open the three-dot menu, select **Custom repositories**, and add
   `https://github.com/ACasset/home-assistant-usb-android-debug-bridge` with type
   **Integration**. This repository is not in the default HACS catalog.
2. Download **Android Debug Bridge USB** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration** and search for
   **Android Debug Bridge USB**. Confirm setup once for the host.
4. Connect a phone with USB debugging enabled. Discovery runs every 30 seconds.
   Each phone gets a device with **USB connected** and **ADB ready** binary sensors.

For manual installation, copy `custom_components/android_debug_bridge_usb` into
your configuration directory's `custom_components` folder and restart.

## USB and phone setup

1. Enable Android Developer options (usually tap **Build number** seven times),
   then enable **USB debugging**.
2. Connect a USB **data** cable to the Home Assistant host. For a VM, pass the
   USB device through to the VM.
3. Ensure the Core process can access `/dev/bus/usb` and a system `libusb` library.
   Home Assistant Container needs USB device passthrough and suitable device
   permissions, including permission for devices reconnected after startup.
   HACS cannot grant USB permissions or install host system libraries.
   On Home Assistant OS, if enumeration fails, check Core's USB access.
4. Open **Developer tools → Actions**, run
   `android_debug_bridge_usb.list_devices`, and note the phone's `serial`.
5. Run `android_debug_bridge_usb.shell` with that serial and command
   `getprop ro.product.model`. Unlock the phone and accept **Allow USB debugging?**.
   If authorization times out, approve and retry.

**USB connected** reports USB presence, not ADB authorization. Disconnected
phones turn off; enumeration failures make sensors unavailable. A phone whose
serial is unreadable or shared with another connected phone is ignored (with a
warning in the logs) rather than targeted ambiguously; other phones keep working.

**ADB ready** (diagnostic) is on when the connected phone trusts this host and
answers a harmless `echo`. It is checked when the phone appears, then every five
minutes, and on every scan while it is off. The check never opens the
authorization prompt on the phone. If the phone no longer trusts the host (for
example after an Android update or **Revoke USB debugging authorizations**), a
repair issue appears under **Settings → Repairs**; run the `shell` action and
accept the prompt to fix it. Use this sensor in an automation to be warned
*before* you need the phone.

## Actions

Actions are registered when the integration loads, even if USB access is broken
at that moment: they then fail with "The USB ADB hub is not loaded" instead of
being unknown to your automations.

`android_debug_bridge_usb.list_devices` refreshes discovery immediately and
returns `{"devices": [{"serial": "ABC123", "port_path": [1, 2]}]}`.

`android_debug_bridge_usb.shell` accepts an exact `serial` and a `command`
(1-8192 characters, without `adb shell`). It optionally returns
`{"output": "...", "exit_code": 0}`. `exit_code` is the Android exit status of
the command (0 means success), or `null` if the phone did not report it. The
action does not fail on a nonzero status; test it in your automation, for
example `{{ phone_result.exit_code == 0 }}`. To report the status, the command
runs in a subshell.
For example, in a script:

```yaml
sequence:
  - action: android_debug_bridge_usb.shell
    data:
      serial: ABC123
      command: getprop ro.product.model
    response_variable: phone_result
  - action: persistent_notification.create
    data:
      title: Phone model
      message: "{{ phone_result.output }}"
```

Other commands include `input keyevent KEYCODE_HOME`, `input text Hello`, and
`dumpsys battery`. Actions are administrator-only for user-initiated calls;
automations and scripts can also call them. Only let trusted users edit scripts
and automations that send commands to your phone.

### Changing USB tethering

Use `svc usb setFunctions rndis` to enable tethering or
`svc usb setFunctions none` to disable it, subject to the phone's permissions.
Changing USB functions can disconnect ADB before it finishes returning the
response, even when Android has applied the change.

For these two standalone commands only, a transport error triggers up to five
reconnection checks with `svc usb getFunctions`, separated by one second.
The command is **never resent**. If the reported mode matches (ignoring the ADB
function), the action returns `{"output": "", "exit_code": null}` and logs the recovery. Otherwise,
it reports an unconfirmed outcome: check the phone before retrying. Keep USB
debugging enabled and ensure VM/container USB access survives re-enumeration.
Compound shell commands and other commands retain normal error handling.

### SMS alerts when internet is lost

`android_debug_bridge_usb.send_sms` drives the phone's SMS app over USB, not the
internet. **It is UI automation, not a reliable SMS API or delivery receipt.**
Do not use it as the sole channel for critical alerts. Each attempt:

1. Wakes the screen, dismisses a swipe-only lock screen, and checks the screen is on.
2. Opens the SMS app on a fresh composer (`ACTION_SENDTO`) with your message.
3. Reads the screen (`uiautomator dump`) and requires a text field of the SMS app
   holding **exactly** your message. If it holds something else, such as a draft
   left by an earlier failed attempt, that text is deleted and the composer is
   reopened once. If the message still is not there (wrong screen, dialog, app
   chooser, or an app that ignores prefilled text), it stops: nothing is tapped.
4. Taps Send once. The button is located on screen by its identifier; if it
   cannot be identified, `send_x`/`send_y` are used, but only if they point at a
   button of the SMS app. Otherwise it stops without tapping.
5. Reads the screen again. If your message is still in the composer, the action
   fails with "probably not sent".

It returns `{"status": "send_button_tapped", "delivery_confirmed": false,
"send_button": "detected", "composer_cleared": true}`. `send_button` is
`detected` or `coordinates`; `composer_cleared` is `true` when the message left
the composer, or `null` if the screen could not be re-read after the tap.
Neither proves that the network accepted or delivered the SMS.

If screen reading does not work on your phone, set `verify_ui: false` to tap
`send_x`/`send_y` blindly after the wait, without any of the checks above.

Prepare a dedicated phone:

1. Insert an active SIM with SMS service and cellular coverage. Choose the default
   SMS app and SIM, dismiss initial dialogs, and disable RCS/chat for this workflow.
   Carrier charges and multipart SMS charges may apply.
2. Keep the phone powered, USB debugging authorized, and without a PIN, pattern
   or password: the action **does not bypass a secure lock screen**. A phone
   without a lock has security implications; restrict physical access.
3. Test `send_sms` manually with your own number and `confirm_ui_risk: true`,
   first without coordinates. If it reports that the Send button cannot be
   identified, open a test message, enable Android Developer options'
   **Pointer location** to find the button's X/Y pixel coordinates, and pass them
   as `send_x` and `send_y`. Increase `wait_seconds` (1-10, default 3) if the
   composer is slow to open.
   Verify the recipient, message, actual SMS receipt, and behavior after sleep.
4. Test again with the regular internet disconnected and Wi-Fi off on the phone.
   ADB still works over USB; cellular SMS service must remain available.

Example automation, using an **existing internet-connectivity binary sensor**
(replace its entity ID, phone serial and recipient; `send_x`/`send_y` are only
needed if your Send button is not identified automatically):

```yaml
alias: SMS when internet is lost
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.internet_connection
    from: "on"
    to: "off"
    for: "00:02:00"
actions:
  - action: android_debug_bridge_usb.send_sms
    data:
      serial: ABC123
      recipient: "+15551234567"
      message: "Home Assistant: regular internet has been offline for 2 minutes."
      send_x: 980
      send_y: 1820
      wait_seconds: 3
      confirm_ui_risk: true
    response_variable: sms_attempt
```

The integration does not detect internet outages itself. Configure an appropriate
connectivity sensor separately (for example using Home Assistant's Ping
integration against an external host; a single host can fail independently of
your internet connection). The example sends once per on-to-off transition after
two minutes, not periodically. It does not fire solely because Home Assistant
starts while offline. No automatic retries are performed: check the phone before
retrying to avoid duplicate SMS. Neither command output, a tap, nor an emptied
composer proves recipient correctness or delivery. For reliable unattended SMS, prefer an Android
app with SMS permission or a supported cellular modem.

## Security and troubleshooting

Only send trusted commands: Android shell access can modify or delete phone data.
There is no exposed network port.

The ADB identity is stored in
`<config>/.storage/android_debug_bridge_usb/adbkey` and `adbkey.pub`, with the
private key restricted to its owner. Treat configuration backups as sensitive.
To revoke access, use **Revoke USB debugging authorizations** on Android.
Removing the integration leaves its key files intact for reinstallation; delete
those two files manually while the integration is unloaded to reset its identity.

Operations are serialized and connections close after each command. Commands have
a 60-second timeout and a 1 MiB output limit (the library buffers output before
this check). The action succeeds even when the Android exit status is nonzero;
check `exit_code`.
This is a text shell interface: desktop `adb install`, `adb push`, file transfers,
interactive terminals, and binary output are not supported.

If no device appears, check the cable, USB debugging, VM/container passthrough,
Core's USB permissions, and competing ADB tools. Failures are reported as action
errors or unavailable entities, with details in Home Assistant logs.
USB support in `adb-shell` is experimental and may vary by phone and host.

If setup reports `No module named 'usb1'` after a Home Assistant update, update
this integration in HACS (or replace its files for a manual installation) and
restart Home Assistant. The integration explicitly requires `libusb1`, the Python
package providing `usb1`: Home Assistant's dependency check can skip the USB extra
when `adb-shell` is already installed. No ADB key reset or integration removal is
needed. This Python dependency is separate from the host system `libusb` library.

## Development

Install the system `libusb-1.0` library (on Debian/Ubuntu,
`apt-get install libusb-1.0-0`) and `requirements-dev.txt` in a Python 3.13 virtual
environment, then run:

```sh
python -m unittest discover -s tests -v
```

The tests mock USB hardware. Real USB enumeration and Android authorization must
also be verified on the Home Assistant host with a connected phone.

GitHub Actions runs these tests, Home Assistant's `hassfest` and the HACS
validation on every pull request, on pushes to `main`, and weekly.
