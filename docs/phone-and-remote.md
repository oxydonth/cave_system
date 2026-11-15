# Phone, presence and access from outside

This guide gets the Home Assistant app onto your phone, makes the heating set back when nobody is home, and lets you reach the system from outside without any cloud service. Do the parts in this order. It assumes the system already runs at home and you can open it in a browser on port 8123.

Menu paths are from the Home Assistant documentation and AVM's FRITZ!OS help as of November 2026. Menus differ slightly between FRITZ!OS versions and phone systems; if a label differs, look for the closest one.

## 1. Install the app

The official Home Assistant app is free and open source. It needs iOS 16.4 or newer, or Android 6 or newer. Download it for your phone from the [companion app page](https://companion.home-assistant.io/) (App Store or Google Play).

1. Connect the phone to your home Wi-Fi.
2. Open the app and choose **Connect to my Home Assistant server**.
3. The app searches the network. If it finds your server, pick it. If not, tap **Enter address manually** and enter `http://<server address>:8123`, where the address is the server's IP address or name on your home network.
4. Log in with your Home Assistant account.
5. Give the phone a name. Home Assistant uses it for the phone's entities and for its notify service (part 2). Pick something plain, such as `pixel_8`.
6. Grant the permissions. Android: location "all the time" and "Nearby devices". iOS: "Allow While Using the App", then "Allow always". Allow notifications when asked (on iOS you are also asked about critical notifications).
7. If you entered an unencrypted address like the one above, the app asks how to treat it. Choose **Most secure**: the app then uses the unencrypted address only on your home network, and asks you to confirm the Wi-Fi network (it pre-fills the current one).

Do not use Home Assistant's "Trusted Networks" login for the app; the app would then work only at home.

Away from home the app needs the VPN of part 4; until you set it up it works only on the home network.

## 2. Notifications on the phone

The system sends every notification through one script, `script.cave_notify`. It always creates a notification inside Home Assistant. It also sends one to your phone when the helper `input_text.cave_notify_service` holds the name of the phone's notify service.

The name of the service is `notify.mobile_app_` followed by the phone's device ID. By default the device ID is the name you gave the phone, with spaces and other special characters replaced by underscores. The phone `pixel_8` gives `notify.mobile_app_pixel_8`.

To find the exact name:

- The device ID is shown in the app's settings in the Home Assistant interface (**Settings**, **Companion app**). If in doubt, take the phone's own name (iOS: **Settings**, **General**, **About**; Android: **Settings**, **About phone**) and replace spaces and special characters with underscores.
- The service exists only if you allowed notifications when you set up the app.

To enter it:

1. Open **Settings**, **Devices & services**, **Helpers**.
2. Open the helper **cave_notify_service** and enter the service name, for example `notify.mobile_app_pixel_8`. The helper holds one service name.
3. To test, run `script.cave_notify` from **Developer tools**, **Actions**, with a `message`. The phone should show it within seconds.

Notifications to a phone away from home pass through Apple's or Google's push service. This is the only place the internet is involved. If it is down, the notification still appears in Home Assistant.

## 3. Presence: setting the heating back when nobody is home

Your FRITZ!Box knows which devices are on the Wi-Fi. The **FRITZ!Box Tools** integration in Home Assistant reads this locally and exposes each device as a device tracker, `home` while it is connected. The heating uses the trackers you list under `presence`.

If you gave the FRITZ!Box credentials in the `.env` file, the first-run setup (`./cave bootstrap`) has already added the integration. If not, add it under **Settings**, **Devices & services**, **Add integration**, **FRITZ!Box Tools**.

### 3.1 Prepare the FRITZ!Box

Both settings below must be on. In the FRITZ!Box interface go to **Home Network**, **Network**, **Network settings**, then **Access Settings in the Home Network**:

- **Permit access for apps** (TR-064)
- **Transmit status information over UPnP**

The Home Assistant documentation also recommends a separate FRITZ!Box user for Home Assistant, with the **FRITZ!Box Settings** permission. Use its name and password in `.env` or in the integration's setup.

### 3.2 Keep the phone's Wi-Fi address fixed

The FRITZ!Box recognises a phone by its Wi-Fi hardware address (MAC). If the phone uses a changing "private" address for your home network, presence breaks. This comes from Home Assistant community advice, not the documentation: for your home Wi-Fi only, on iOS turn off **Private Wi-Fi Address** (Wi-Fi settings, the (i) next to the network); on Android choose **Privacy**, **Use device MAC** in the network's settings.

### 3.3 Enable the phone's tracker

Trackers for newly detected devices are disabled until you switch them on. To enable yours:

1. Make sure the phone has been on the Wi-Fi so the FRITZ!Box has seen it.
2. Open **Settings**, **Devices & services**, **Entities**. Remove any filter that hides disabled entities, and search for `device_tracker`.
3. Open your phone's tracker, then its settings (the cog), and switch **Enabled** on. Wait for Home Assistant to reload it.
4. Note the entity ID, for example `device_tracker.max_phone`. Check that it shows `home`; turn the phone's Wi-Fi off and on to see it change (it can take a few minutes to show `not_home`).

If your tracker is already enabled, skip step 3. Home Assistant release 2026.11 makes all network trackers disabled by default; community reports say FRITZ!Box Tools already behaved so.

### 3.4 Tell the heating

Edit `cave/heating.yaml` in the server's state directory (`/var/lib/cave/homeassistant/cave/heating.yaml` on a standard install) and list the phones of everybody who lives there:

```yaml
presence:
  - device_tracker.max_phone
  - device_tracker.anna_phone
away_delay: "00:30:00"
```

Then run `./cave apply` in the repository on the server. It validates the file, and if it is valid, puts it into effect; if not, it shows the errors and changes nothing. Someone counts as home if any listed tracker says so. With no `presence` entry, the heating treats the home as always occupied.

### 3.5 What the away delay does

When every listed phone is gone, nothing happens at once. Only after nobody has been home for the whole `away_delay` (30 minutes in the starter file) does the home count as empty and every room drop to the away temperature. The moment a phone returns, the rooms go back to their schedule.

The delay is there because phones drop off the Wi-Fi when they sleep, and the FRITZ!Box reports them as gone. The FRITZ!Box Tools integration has its own option, **consider home** (seconds before a disconnected device counts as not at home), under the integration's options; the two together decide how long a phone can be quiet. A phone that sleeps for ten minutes therefore does not set the heating back. A boost or an override you set by hand is not cancelled when presence turns to away, for the same reason.

If the tracker is unavailable or missing, the system assumes you are home.

## 4. Access from outside: the FRITZ!Box VPN

No port is opened to the internet and no cloud is involved. You connect the phone to your FRITZ!Box with its built-in WireGuard VPN, and the app then uses the same address as at home.

### 4.1 What you need

- FRITZ!OS 7.50 or newer. The cable models FRITZ!Box 6490 and 6590 do not support WireGuard.
- A MyFRITZ! account with the FRITZ!Box registered, or another dynamic DNS name set up in the FRITZ!Box. AVM states that WireGuard works only together with one of them. If both are set, the MyFRITZ! address is used.
- Either a public IPv4 address, or IPv6 (see 4.3).
- A home network whose address range differs from the one the phone will be in. If you join a Wi-Fi that uses the same range as your FRITZ!Box (often 192.168.178.x), the VPN stops working; change the range under **Home Network** if this is a problem.

### 4.2 Set it up

On the FRITZ!Box, in your browser:

1. Open **Internet**, **Permit Access** (German interface: **Internet**, **Freigaben**), and the tab **VPN (WireGuard)**.
2. Click **Add WireGuard Connection** (**Verbindung hinzufügen**), then **Connect a single device** (**Einzelgerät verbinden**), then **Next**.
3. Name the connection, for example `Anna's iPhone`, and finish. The box shows a QR code. Keep the page open.

Make one connection per phone.

On the phone:

1. Install the free **WireGuard** app.
2. Open it, tap the plus, and scan the QR code. Name the tunnel and save it.
3. Switch the tunnel on. A VPN symbol appears in the status bar.
4. In the Home Assistant app, with the phone on mobile data, open the app. It should reach the server at the same address as at home.

Do the phone steps while the QR code is on screen; the box offers the configuration only then.

### 4.3 If your provider gives no public IPv4 (DS-Lite)

Many cable and fibre lines in Germany use DS-Lite: your FRITZ!Box has no public IPv4 address of its own, so nothing can reach it over IPv4. WireGuard still works, over IPv6, if:

- the FRITZ!Box has a public IPv6 address (it normally does on such lines), and
- the phone itself has IPv6 on the network it is in.

The MyFRITZ! address then resolves to the IPv6 address automatically. Mobile networks that support IPv6 work. A Wi-Fi at someone else's home or a company that gives only IPv4 does not: from there the VPN cannot connect, and you cannot reach the system until you are on another network.

## 5. What depends on the internet

| Works with the internet unplugged | Needs the internet |
|---|---|
| Every heating control: schedules, window pause, away, boost | Updating the system (the server pulls releases from GitHub) |
| The web UI and the phone app at home | Push notifications to a phone outside the home (through Apple's or Google's service) |
| Presence through the FRITZ!Box | The VPN from outside, and MyFRITZ! name lookup |
| Notifications inside Home Assistant | |
| Radiators keep their last setpoint and frost protection even if the server is off | |

## Sources

- [Home Assistant Companion App: getting started](https://companion.home-assistant.io/docs/getting_started/)
- [Companion App: basic notifications](https://companion.home-assistant.io/docs/notifications/notifications-basic/)
- [Home Assistant: FRITZ!Box Tools integration](https://www.home-assistant.io/integrations/fritz/)
- [Home Assistant developer blog: ScannerEntity, 28 October 2026](https://contributors.home-assistant.io/blog/2026/10/28/scanner-entity-devices/)
- [Home Assistant community: private addresses and presence](https://community.home-assistant.io/t/presence-with-ios-private-address/436628)
- [AVM: WireGuard VPN to the FRITZ!Box on a smartphone or tablet](https://fritz.com/service/vpn/wireguard-vpn-zur-fritzbox-am-smartphone-oder-tablet-einrichten/)
- [AVM: VPN with the FRITZ!Box](https://fritz.com/en/pages/vpn-with-fritz-box)
