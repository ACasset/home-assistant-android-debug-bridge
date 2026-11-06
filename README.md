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
   Each phone gets a device and a **USB connected** binary sensor.

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

The connection sensor reports USB presence, not ADB authorization. Disconnected
phones turn off; enumeration failures make sensors unavailable. Phones with missing
or duplicate serials are rejected rather than targeted ambiguously.

## Actions

`android_debug_bridge_usb.list_devices` refreshes discovery immediately and
returns `{"devices": [{"serial": "ABC123", "port_path": [1, 2]}]}`.

`android_debug_bridge_usb.shell` accepts an exact `serial` and a `command`
(1-8192 characters, without `adb shell`). It optionally returns `{"output": "..."}`.
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
function), the action returns `{"output": ""}` and logs the recovery. Otherwise,
it reports an unconfirmed outcome: check the phone before retrying. Keep USB
debugging enabled and ensure VM/container USB access survives re-enumeration.
Compound shell commands and other commands retain normal error handling.

### SMS alerts when internet is lost

`android_debug_bridge_usb.send_sms` uses `ACTION_SENDTO` to open a message and
then `input tap X Y` to press Send once. It uses USB locally, not the internet.
**It is UI automation, not a reliable SMS API or delivery receipt.** It returns
`{"status": "send_button_tapped", "delivery_confirmed": false}`. A wrong screen,
dialog, locked phone, or changed layout can prevent sending or tap something else.
Do not use it as the sole channel for critical alerts.

Prepare a dedicated phone:

1. Insert an active SIM with SMS service and cellular coverage. Choose the default
   SMS app and SIM, dismiss initial dialogs, and disable RCS/chat for this workflow.
   Carrier charges and multipart SMS charges may apply.
2. Keep the phone powered, USB debugging authorized, and the screen unlocked.
   The action wakes the screen but **does not bypass a lock screen**. Leaving a
   phone unlocked has security implications; restrict physical access.
3. Open a test message with the same SMS app. Enable Android Developer options'
   **Pointer location** to find the Send button's X/Y pixel coordinates.
   Keep orientation, display scale, keyboard state, and app layout consistent.
4. Test `send_sms` manually with your own number. Set `send_x`, `send_y`, and
   `confirm_ui_risk: true`; increase `wait_seconds` (1-10, default 3) if needed.
   Verify the recipient, message, actual SMS receipt, and behavior after sleep.
   Some SMS apps ignore the prefilled body: do not automate unless yours works.
5. Test again with the regular internet disconnected and Wi-Fi off on the phone.
   ADB still works over USB; cellular SMS service must remain available.

Example automation, using an **existing internet-connectivity binary sensor**
(replace its entity ID, phone serial, recipient, and coordinates):

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
retrying to avoid duplicate SMS. Neither command output nor a tap proves sending,
recipient correctness, or delivery. For reliable unattended SMS, prefer an Android
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
this check). Successful completion does not guarantee Android exit status zero.
This is a text shell interface: desktop `adb install`, `adb push`, file transfers,
interactive terminals, and binary output are not supported.

If no device appears, check the cable, USB debugging, VM/container passthrough,
Core's USB permissions, and competing ADB tools. Failures are reported as action
errors or unavailable entities, with details in Home Assistant logs.
USB support in `adb-shell` is experimental and may vary by phone and host.

## Development

Install the system `libusb-1.0` library (on Debian/Ubuntu,
`apt-get install libusb-1.0-0`) and `requirements-dev.txt` in a Python 3.13 virtual
environment, then run:

```sh
python -m unittest discover -s tests -v
```

The tests mock USB hardware. Real USB enumeration and Android authorization must
also be verified on the Home Assistant host with a connected phone.
