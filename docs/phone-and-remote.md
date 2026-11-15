# Phone, presence and access from outside

This guide gets the Home Assistant app onto your phone, makes the heating set back when nobody is home, and lets you reach the system from outside without any cloud service. Do the parts in this order. It assumes the system already runs at home and you can open it in a browser on port 8123. If you have not installed it yet, read part 4.1 first: one FRITZ!Box setting is much easier to change before the install than after.

Menu paths are from the Home Assistant documentation as of November 2026 and from AVM's knowledge base articles of early 2025. Menus differ slightly between FRITZ!OS versions and phone systems; if a label differs, look for the closest one.

## 1. Install the app

The official Home Assistant app is free and open source. It needs iOS 16.4 or newer, or Android 6 or newer. Download it for your phone from the [companion app page](https://companion.home-assistant.io/) (App Store or Google Play).

1. Connect the phone to your home Wi-Fi.
2. Open the app and choose **Connect to my Home Assistant server**.
3. The app searches the network. If it finds your server, pick it. If not, tap **Enter address manually** and enter `http://<server address>:8123`, where the address is the server's IP address or name on your home network.
4. Log in with your Home Assistant account.
5. Name the phone. The field is filled in with the phone's own name; keep it or change it. Home Assistant builds the phone's entities and its notify action from this name (part 2).
6. Grant the permissions. Android: location "all the time" and "Nearby devices". iOS: "Allow While Using the App", then "Allow always". Allow notifications when asked.
7. If you entered an `http://` address, the app asks for a connection security level. What you choose decides whether the app works over the VPN of part 4:
   - **Android:** choose **Most secure** and confirm your home Wi-Fi. After part 4, also open **Settings**, **Companion app**, your server, **Home network** in the app and tick **VPN connected**.
   - **iPhone:** iOS recognises "home" only by the Wi-Fi name, so **Most secure** would show a block screen whenever you are not on your home Wi-Fi, also over the VPN. Choose **Less secure**. The app's unencrypted connection is then allowed on every network, so in part 4 you make sure that away from home it only ever goes through the VPN tunnel.

Away from home the app needs the VPN of part 4; until you set it up it works only on the home network.

## 2. Notifications on the phone

The system sends every notification through one script, `script.cave_notify`. It always creates a notification inside Home Assistant. It also sends one to your phone when the helper `input_text.cave_notify_service` holds the name of the phone's notify action. The helper holds one name, so only one phone receives notifications; a second phone sees them only inside Home Assistant.

To find the name:

1. In Home Assistant, open **Developer tools**, **Actions**.
2. Type `notify.mobile_app` in the action field. The list shows one action per phone with the app installed, for example `notify.mobile_app_pixel_8`. Copy that name.

The name is `notify.mobile_app_` followed by the phone's device ID, which is the name from step 5 of part 1, pre-filled with the phone's own name (iOS: **Settings**, **General**, **About**; Android: **Settings**, **About phone**) with spaces and special characters replaced by underscores. The action exists only if you allowed notifications when you set up the app.

To enter it:

1. Open **Settings**, **Devices & services**, **Helpers**.
2. Search for `input_text.cave_notify_service` and open it. Enter the full name, for example `notify.mobile_app_pixel_8`, and save. What you enter stays across restarts and updates.
3. To test, run `script.cave_notify` from **Developer tools**, **Actions** with a `title` and a `message`. The phone should show it within seconds.

Notifications to a phone away from home pass through Apple's or Google's push service. This is the only place the internet is involved. If it is down, the notification still appears in Home Assistant.

## 3. Presence: setting the heating back when nobody is home

Your FRITZ!Box knows which devices are on the Wi-Fi. The **FRITZ!Box Tools** integration in Home Assistant reads this locally and exposes each device as a device tracker, `home` while it is connected. The heating uses the trackers you list under `presence`.

### 3.1 Prepare the FRITZ!Box

Do this before `./cave bootstrap`, because the integration's setup needs it.

1. In the FRITZ!Box interface go to **Home Network**, **Network**, **Network settings**, then **Access Settings in the Home Network**, and switch on both **Permit access for apps** (TR-064) and **Transmit status information over UPnP**.
2. Create a separate user for Home Assistant: **System**, **FRITZ!Box Users**, **Users**, **Add User**. Give it both the **FRITZ!Box Settings** permission (FRITZ!Box Tools needs it) and the **Smart Home** permission (the FRITZ!SmartHome integration needs it; without it a FRITZ!Smart Thermo never appears). The two integrations share one login, the one in `.env` (`CAVE_FRITZ_USER`, `CAVE_FRITZ_PASSWORD`).

If you gave the credentials in `.env`, `./cave bootstrap` adds the integration. If you ran it before doing the steps above, or gave no credentials, add FRITZ!Box Tools yourself under **Settings**, **Devices & services**, **Add integration**.

### 3.2 Keep the phone's Wi-Fi address fixed

The FRITZ!Box recognises a phone by its Wi-Fi hardware address (MAC). If the phone uses a changing "private" address for your home network, presence breaks. This is Home Assistant community advice, not documentation: for your home Wi-Fi only, on iOS set **Private Wi-Fi Address** to **Fixed** or off (Wi-Fi settings, the (i) next to the network); on Android choose **Privacy**, **Use device MAC** in the network's settings. Do this before the next step: a new address creates a new tracker.

### 3.3 Enable the phone's tracker

Your phone's tracker may have been created disabled; from Home Assistant 2026.11 every new network tracker is. To enable it:

1. Make sure the phone has been on the Wi-Fi so the FRITZ!Box has seen it.
2. Open **Settings**, **Devices & services**, **Entities**. Remove any filter that hides disabled entities, and search for `device_tracker`.
3. Open your phone's tracker, then its settings (the cog), and switch **Enabled** on. Wait for Home Assistant to reload it.
4. Note the entity ID, for example `device_tracker.max_phone`. Check that it shows `home`; turn the phone's Wi-Fi off and on to see it change (it can take a few minutes to show `not_home`).

If the tracker is already enabled, skip step 3.

### 3.4 Tell the heating

Edit `cave/heating.yaml` in the server's state directory (`/var/lib/cave/homeassistant/cave/heating.yaml` on a standard install). Add or change these two keys at the top level and leave the rest of the file as it is; do not replace the file:

```yaml
presence:
  - device_tracker.max_phone
  - device_tracker.anna_phone
away_delay: "00:30:00"
```

List the phones of everybody who lives there. Then run `./cave apply` in the repository on the server. It validates the file and, if it is valid, puts it into effect; if not, it shows the errors, names the room and field, and changes nothing, so fix the line it names and run it again. Someone counts as home if any listed tracker says so. With no `presence` entry, the heating treats the home as always occupied.

### 3.5 What the away delay does

When every listed phone is gone, nothing happens at once. Only after nobody has been home for the whole `away_delay` (30 minutes in the example) does the home count as empty and every room drop to the away temperature. The moment a phone returns, the rooms go back to their schedule.

The delay is there because phones drop off the Wi-Fi when they sleep, and the FRITZ!Box then reports them as gone. The FRITZ!Box Tools integration has its own option, **consider home** (seconds before a disconnected device counts as not at home), under the integration's options; the two together decide how long a phone can be quiet. A phone that sleeps for ten minutes therefore does not set the heating back. A boost or an override you set by hand is not cancelled when presence turns to away, for the same reason.

If the tracker is unavailable or missing, the system assumes you are home.

## 4. Access from outside: the FRITZ!Box VPN

No port is opened to the internet and no cloud is involved. You connect the phone to your FRITZ!Box with its built-in WireGuard VPN, and the app then uses the same address as at home. The AVM articles this part follows are listed under Sources as archived copies, because the live AVM knowledge base needs JavaScript; labels on your box may differ slightly.

### 4.1 Preparation

**Check that your FRITZ!Box offers WireGuard.** Open **Internet**, **Permit Access**. If there is no tab **VPN (WireGuard)**, update FRITZ!OS; if it is still missing, your model does not support WireGuard (the cable models 6490 and 6590 are reported not to).

**Register with MyFRITZ!.** AVM states that the FRITZ!Box supports WireGuard only together with a MyFRITZ! or dynamic DNS address. If both are set up, the MyFRITZ! address is used.

1. Open **Internet**, **MyFRITZ! Account**.
2. Enter your email address and click **Apply**.
3. Open the email from MyFRITZ!Net and click **Register Your FRITZ!Box**. If no email arrives, look in the spam folder.

**Change the FRITZ!Box's address range, if needed. Do it before you install the system.** Both ends of the VPN must be in different IP networks. All FRITZ!Boxes ship with 192.168.178.x. If the phone is in a network that uses the same range (another FRITZ!Box, a friend's home), the VPN does not work. AVM tells you to change your own range, for example to 192.168.10.1 with subnet mask 255.255.255.0:

1. Open **Home Network**, **Network**, the tab **Network Settings**.
2. In the section LAN Settings, click **Additional Settings**, then **IPv4 Settings**.
3. Enter the new address and subnet mask, click **Apply**, and confirm on the FRITZ!Box if asked.

On a cable box do not use 192.168.100.x: it is reserved for the provider.

Changing the range renumbers every device in the home. Before you install, do it first and then give the server and the coordinator fixed addresses. If the system is already running, find the new addresses in the FRITZ!Box under **Home Network**, **Network**, and update:

- the coordinator address in `.env` (`CAVE_Z2M_SERIAL_PORT=tcp://<address>:6638`); otherwise every Zigbee device is lost;
- `CAVE_FRITZ_HOST` in `.env`;
- the FRITZ!Box Tools and FRITZ!SmartHome integrations, under **Settings**, **Devices & services** (three-dot menu of the integration, **Reconfigure**, new host);
- the server address saved in the phone app, and any bookmark;
- then recreate the containers with `./cave up` so they read `.env` again.

### 4.2 Set it up

On the FRITZ!Box, in your browser:

1. Open **Internet**, **Permit Access** (German interface: **Internet**, **Freigaben**), and the tab **VPN (WireGuard)**.
2. Click **Add Connection** (**Verbindung hinzufügen**), then **Connect a single device** (**Einzelgerät verbinden**), then **Next**.
3. Enter a name for the connection, for example `Annas iPhone`, and click **Finish**.
4. If the FRITZ!Box asks you to confirm the procedure, confirm it on the box (press a button or confirm by phone), then click **OK**. Until you do, the page waits for you.

The box shows a QR code. Scan it before you close the page; if you closed it too early, delete the connection and create it again. Make one connection per phone.

On the phone:

1. Install the free **WireGuard** app.
2. Open it, tap the plus, and choose scanning a QR code. Scan the code. If you cannot scan it, the box also offers **Download Settings**, and the app can import that file. Name the tunnel and tap **Create Tunnel** or **Save**.
3. Tap the on/off switch of the tunnel. A VPN symbol appears in the status bar.
4. Open the Home Assistant app on mobile data. Then:
   - **Android:** if it shows a block screen, check that **VPN connected** is ticked under **Settings**, **Companion app**, your server, **Home network** (part 1, step 7).
   - **iPhone:** in the WireGuard app, edit the tunnel and switch on **On-Demand activation** for mobile data and for every Wi-Fi except your own, so that away from home the app's unencrypted connection goes only through the tunnel. If the Home Assistant app still does not connect, enter the same address as the **External URL** in the app (**Settings**, **Companion app**, your server). Whether iOS needs that with **Less secure** is not documented; try it only if it fails.

### 4.3 If your provider gives no public IPv4 (DS-Lite)

Many cable and fibre lines in Germany use DS-Lite: your FRITZ!Box has no public IPv4 address of its own, so nothing can reach it over IPv4. AVM states the VPN needs the FRITZ!Box to have an IPv4 or IPv6 address reachable from the internet, and the phone to be able to reach that address. On a DS-Lite line that means IPv6:

- the FRITZ!Box has a public IPv6 address (normally it does), and
- the phone has IPv6 on the network it is in. Mobile networks that moved from IPv4 to IPv6 work (AVM names Deutsche Telekom as an example). A Wi-Fi hotspot or company network that offers only IPv4 does not: from there the VPN cannot connect.

The MyFRITZ! address from 4.1 is what the phone connects to. To find out whether your line is DS-Lite, look at the connection type on the FRITZ!Box overview page.

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
- [Companion App: connection security level](https://companion.home-assistant.io/docs/getting_started/connection-security-level/)
- [Companion App: basic notifications](https://companion.home-assistant.io/docs/notifications/notifications-basic/)
- [Home Assistant: FRITZ!Box Tools integration](https://www.home-assistant.io/integrations/fritz/) and [FRITZ!SmartHome integration](https://www.home-assistant.io/integrations/fritzbox/)
- [Home Assistant developer blog: ScannerEntity, 28 October 2026](https://contributors.home-assistant.io/blog/2026/10/28/scanner-entity-devices/)
- [Home Assistant community: private addresses and presence](https://community.home-assistant.io/t/presence-with-ios-private-address/436628)
- AVM knowledge base, archived copies from the Wayback Machine:
  - [3684, Setting up a WireGuard VPN to the FRITZ!Box on a smartphone or tablet](https://web.archive.org/web/20250217015418/https://en.avm.de/service/knowledge-base/dok/FRITZ-Box-6690-Cable/3684_Setting-up-a-WireGuard-VPN-to-the-FRITZ-Box-on-a-smartphone-or-tablet/)
  - [3711, WireGuard-VPN mit fester IP-Adresse nutzen](https://web.archive.org/web/20250126130101/https://avm.de/service/wissensdatenbank/dok/FRITZ-Box-7590/3711_WireGuard-VPN-mit-fester-IP-Adresse-nutzen/)
  - [3718, WireGuard kann keine VPN-Verbindung zur FRITZ!Box herstellen](https://web.archive.org/web/20250211172659/https://avm.de/service/wissensdatenbank/dok/FRITZ-Box-7590-AX/3718_WireGuard-kann-keine-VPN-Verbindung-zur-FRITZ-Box-herstellen/)
