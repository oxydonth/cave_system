# What to buy

This guide tells you what to buy for cave_system, how many, and in what order. The products and prices come from the research in [concept section 3](concept.md#3-hardware), done on 15 November 2026. Prices are German street prices from that month; check them again before you order. The sources are at the end.

## The shopping list in order

Buy in three steps. Do not equip every radiator on the first day.

| Step | Buy | Why now |
|---|---|---|
| 1. The base | The server, the Zigbee coordinator, and a FRITZ!Box you already own | Nothing else works without them. |
| 2. A trial | One or two Sonoff TRV Gen2 thermostats, one Sonoff SNZB-02P room sensor and one SNZB-04P window contact, all for the same room | The Gen2 went on sale on 20 August 2026 and early reports mention connection drop-outs. Check on your own valves that it fits, how loud it is, and that it stays connected for a week or two. |
| 3. The rest | The remaining thermostats, one room sensor per room, one window contact per window you open | Only after the trial went well. If it did not, use the TRVZB or a FRITZ!Smart Thermo 302 for the rest (see the table below). |

## How many of each

| Item | How many |
|---|---|
| Radiator thermostat | One per radiator. A room with two radiators gets two; the system treats the room as one unit. |
| Room temperature and humidity sensor | One per room, placed away from the radiator. The thermostat's own sensor sits next to the hot radiator and reads too high; the room sensor is what makes regulation accurate. A FRITZ! thermostat cannot use it. |
| Window contact | One per window or balcony door that you open. The heating pauses while it is open, so you do not heat the street while airing (Stoßlüften). |
| Zigbee coordinator | One for the whole flat. |
| Server | One. |

## What each product is for

Check the last column before you order.

| Product | For | Price | Check before buying |
|---|---|---|---|
| **Sonoff TRV Gen2** (TRV-ZBT) | Radiator thermostat, the main pick. Regulates on the room sensor, stores three weekly plans on the device (the system does not use them). Sonoff states up to six months on two AA cells. | €31–35 | The valve: see "Will it fit your valve?" below. Buy one or two first. |
| **Sonoff TRVZB** | Radiator thermostat, the fallback. Three years old, larger and louder than the Gen2, but well understood. Regulates on the room sensor. | €25–29 | The same. |
| **Bosch Heizkörper-Thermostat II [+M]** (BTH-RA) | Radiator thermostat for a quiet room, typically a bedroom. Regulates on the room sensor. | about €63 | It ships in Matter mode and must be switched to Zigbee on the device; you pair it with its install code. You do not need the Bosch controller. |
| **FRITZ!Smart Thermo 302** (or the older 301) | Radiator thermostat that talks to your FRITZ!Box over DECT, with no Zigbee involved. For a room Zigbee does not reach, or if you would rather not run Zigbee. Regulates on its own sensor only. | €45–55 | Your FRITZ!Box must have a DECT base station (not every model has one: look for DECT in its specification), must not be a 7312 or 7412, and needs FRITZ!OS 6.83 or later. A new setpoint reaches the valve after about three minutes, up to fifteen on the 301. Prefer the 302. |
| **Sonoff SNZB-02P** | Room temperature and humidity. | €10–12 | Nothing special. |
| **Sonoff SNZB-04P** | Window and door contact. | €10–12 | Nothing special. |
| **SMLIGHT SLZB-06MG24** | Zigbee coordinator, connected by Ethernet (PoE capable). | €40–45 | You need an Ethernet socket, or a PoE switch (power and network through one cable), at the place where you mount it. |

Why the Gen2 is the main pick: heise chose it as its pick for Home Assistant (4.5 of 5 points on 1 November 2026). It has PID regulation, an input for the external room temperature, and on-device weekly plans. It needs Zigbee2MQTT 2.12.1 or newer, which the version this project pins satisfies. The Bosch costs twice as much and is worth it where motor noise matters. The FRITZ!Smart Thermo 302 was rated "gut (1.9)" and price tip by Stiftung Warentest in 2023, under its earlier name FRITZ!DECT 302.

## Will it fit your valve?

German radiator valves almost always have an M30 × 1.5 thread, and every thermostat above screws straight onto it, with one exception (see step 3). How to check:

1. Take off the old thermostat head.
2. Look at the valve body. A thread of 30 mm across the crests with 6 to 7 grooves in 10 mm is M30 × 1.5: it fits directly.
3. A smooth ring with notches and no thread is a Danfoss clamp valve (RA, RAV or RAVL). Measure the outside of the ring: about 23 mm is RA, 25.5 to 26 mm is RAVL, 34 mm is RAV. These need an adapter. A valve marked Danfoss RTD has the M30 × 1.5 thread but still needs an adapter.
4. Any other connection (M28, M30 × 1.0, Vaillant and others): look up your valve and the adapter before buying.

Which adapters come in the box differs by model:

| Model | Adapters in the box |
|---|---|
| Sonoff TRV Gen2 | CAL, GIA, M28, RA, RAV, RAVL |
| Sonoff TRVZB | RA, RAV, RAVL, CAL, GIA and M28 are the adapters Sonoff's compatibility guide names for it; it does not say "in the box", so check the shop page |
| Bosch Thermostat II [+M] | RA and RAVL only |
| FRITZ!Smart Thermo 302 | RA only |

If you have RAV valves, a Bosch does not fit out of the box. If you have RAV or RAVL valves, a FRITZ!Smart Thermo 302 does not fit out of the box. Buy the adapter separately, one per valve, and check that it is made for that thermostat. Sonoff Gen2 owners with M28 valves are covered by the included M28 adapter.

## The server

The system needs about 2 GB of memory and little processor power. What matters is storage that survives constant database writes.

| Option | Price | Verdict |
|---|---|---|
| **Mini PC with Intel N100 or N150**, 8–16 GB RAM, NVMe SSD | €190–280 new; refurbished business mini PCs from about €150 | **Buy this if you need a server.** Real SSD, headroom, 6–8 W idle. |
| **Raspberry Pi 4 or 5 you already own**, 4 GB or more | already owned | **Fine to start on**, with the requirements below. |
| Raspberry Pi 5 bought new | US list price $110 (4 GB) or $175 (8 GB), bare board | Do not buy. After this year's memory price rises a complete mini PC costs about the same, and the Pi's lower idle draw (about 3 W) saves roughly €14 a year. |
| Home Assistant Green | €179 | Do not buy. It is an appliance for Home Assistant OS; this project uses Home Assistant Container. |

If you use a Raspberry Pi it needs:

- 4 GB of memory or more.
- An SSD to boot from (USB 3, or NVMe through a HAT). SD cards wear out under the database writes.
- The official power supply.
- A 64-bit operating system: Raspberry Pi OS Lite 64-bit.

Either way the server runs Debian 13 (on the Pi, Raspberry Pi OS Lite), with Docker Engine, the Compose plugin, Git and the system Python. All images the system uses exist for x86-64 and ARM64. The install guide covers the rest.

## Where the coordinator goes

The coordinator is the radio that links the server to the Zigbee devices. The SLZB-06MG24 is a network coordinator: it reaches the server over Ethernet, not USB, so it does not have to sit next to the server or the router. Put it centrally in the flat, near the middle of the rooms with thermostats, away from the router and server, and give it an Ethernet cable or PoE. Zigbee2MQTT reaches it as `tcp://<address>:6638`; the address goes into the `.env` file. In the FRITZ!Box, give the coordinator and the server fixed addresses (in the FRITZ!Box under Home Network > Network, open the device and look for the option to always give it the same IPv4 address); if the coordinator's address changes, Zigbee stops.

Battery-powered Zigbee devices do not relay for each other. If a room is out of reach after pairing:

1. Add a mains-powered Zigbee plug between the coordinator and that room as a relay.
2. Or use a FRITZ!Smart Thermo 302 in that room. It talks to the FRITZ!Box over DECT, whose range usually covers a whole flat.

Concrete walls are the usual cause. Test range during the trial step, in the room that is farthest away.

## Worked example: a small flat

Three rooms (living room, bedroom, bathroom) and four radiators (two in the living room). Prices at the upper end of the ranges.

| Item | Count | Price each | Total |
|---|---|---|---|
| Sonoff TRV Gen2 | 4 | €35 | €140 |
| Sonoff SNZB-02P (one per room) | 3 | €12 | €36 |
| Sonoff SNZB-04P (one per window that is opened) | 4 | €12 | €48 |
| SMLIGHT SLZB-06MG24 | 1 | €45 | €45 |
| **Thermostats, sensors, coordinator** | | | **about €270** |
| Mini PC, only if you have no server | 1 | about €200 | +€200 |

At the lower end of the ranges the first block is about €235. In this flat, buy first: the coordinator, two Gen2 thermostats, one room sensor and one window contact for the living room. Buy the other two thermostats and the rest of the sensors after the trial. If the bedroom is quiet-critical, swap its Gen2 for a Bosch (+€28 or so per radiator). If the bathroom is out of Zigbee reach, a FRITZ!Smart Thermo 302 (€45–55) replaces its Gen2. It cannot use a Zigbee room sensor, and a Zigbee sensor would not reach that room anyway, so the bathroom loses its humidity reading and the prompt to air the room.

## Considered and not recommended

Reviews of some of these are good. They are left out for the reasons below.

| Product | Reason |
|---|---|
| Aqara W600 | Tops several 2026 comparisons, but has open firmware problems that matter here: with an external sensor selected the valve still regulates on the internal one, and under Zigbee2MQTT the valve opening and heating state are not reported. |
| tado° X | Requires the vendor's cloud account. |
| Homematic IP Evo | Excellent regulation and a test winner, but needs its own hub (HCU or CCU3) and a custom integration to run locally. Worth it for a house with underfloor heating, not for radiators in a flat. |
| Eve Thermo | Fully local over Thread/Matter and a good fit for Apple Home. Costs twice as much, and Matter exposes only the basics to Home Assistant. |
| TP-Link Kasa KE100 | Cheap and local, but a proprietary hub family, so it does not double as the network for sensors and later devices. |

## For tenants

You generally may replace the thermostat heads without asking the landlord, because the valve itself is not touched. Keep the original heads and refit them when you move out. Do not unscrew the valve or the radiator.

## Sources

Facts and prices above are from concept section 3 and its sources, checked on 15 November 2026, except where this guide says otherwise. The valve-recognition steps are from [smarthome-start.de, Heizkörperventil bestimmen](https://smarthome-start.de/2026/09/15/heizkoerperventil-bestimmen-adapter/). The adapter table is from the vendor pages listed under "Adapters".

Thermostats and tests
- [heise: the ten best radiator thermostats, 7 Nov 2026](https://www.heise.de/bestenlisten/testsieger/top-10-das-beste-heizkoerperthermostat-im-test-testsieger-heizt-ohne-cloud/1e43f3r)
- [heise: Sonoff TRV Gen2 review](https://www.heise.de/bestenlisten/testbericht/sonoff-trv-gen2-im-test-guenstiges-heizkoerperthermostat-fuer-home-assistant/wh3ff9g)
- [Sonoff TRV Gen2 with Zigbee2MQTT and ZHA](https://localsmarthomeguide.com/articles/sonoff-trv-gen2-home-assistant-setup/)
- [Sonoff TRVZB external temperature sensor, Zigbee2MQTT discussion](https://github.com/Koenkk/zigbee2mqtt/discussions/26308)
- [Bosch Thermostat II [+M]: switching from Matter to Zigbee](https://www.thing-king.de/bosch-heizkoerper-thermostat-ii-m-von-matter-thread-zu-zigbee-umstellen/) and [pairing with Zigbee2MQTT](https://www.thing-king.de/bosch-smart-home-heizkoerper-thermostat-ii-ueber-zigbee2mqtt-mit-home-assistant-verbinden/)
- [Bosch Thermostat II [+M] prices, Geizhals](https://geizhals.de/bosch-smart-home-heizkoerper-thermostat-ii-m-matter-8750002982-a3337738.html)
- [FRITZ!Smart Thermo 302 prices, heise Preisvergleich](https://preisvergleich.heise.de/fritz-fritz-smart-thermo-302-funk-heizkoerperthermostat-20003120-a3712218.html)
- [FRITZ!Smart Thermo 302 with Home Assistant](https://www.einfachgadgets.de/artikel/fritz-smart-thermo-302-home-assistant)
- [FRITZ!DECT 301/302 command delay, simon42 community](https://community.simon42.com/t/dect-301-bzw-302-die-verzoegerungszeit-umgehen/38542)
- [Stiftung Warentest: smart radiator thermostats](https://www.test.de/Heizkoerperthermostat-Test-5115581-0/) and [ADAC summary](https://www.adac.de/rund-ums-haus/energie/versorgung/smarte-heizkoerperthermostate/)
- [Aqara W600: valve opening and heating state not reported, Zigbee2MQTT issue 33428](https://github.com/Koenkk/zigbee2mqtt/issues/33428) and [external sensor does not control the valve, Aqara forum](https://forum.aqara.com/t/w600-external-sensor-does-not-control-valve-firmware-issue/341137)

Adapters
- [Sonoff TRV Gen2 at Expert4House](https://www.expert4house.com/en/smart-thermostats/thermostatic-valves/sonoff-trv-gen2-trv-zbt-smart-zigbee-thermostatic-radiator-valve): six adapters, RA, RAV, RAVL, GIA, CAL and M28 (the same list appears on heise Preisvergleich under "Besonderheiten")
- [Sonoff TRV compatibility guide, December 2024](https://sonoff.tech/wp-content/uploads/2024/12/SONOFF_TRV-Compatibility-Guide-20241202.pdf)
- [Bosch Heizkörper-Thermostat II, Lieferumfang](https://www.bosch-smarthome.com/de/de/produkte/heizung/heizkoerper-thermostat/): "1 Adapter Danfoss RA, 1 Adapter Danfoss RAVL"
- [FRITZ!Smart Thermo 302, package contents and requirements](https://fritz.com/en/products/fritz-smart-thermo-302): "Adapter for Danfoss RA radiator valve"; "FRITZ!Box with DECT base station (except FRITZ!Box 7312/7412) and FRITZ!OS 6.83 or later"

Sensors and coordinators
- [Sonoff SNZB-02P](https://www.berrybase.de/en/sonoff-snzb-02p-temperature-humidity-sensor-temperatur-luftfeuchte-sensor-zigbee-rund) and [SNZB-04P](https://www.berrybase.de/en/sonoff-snzb-04p-door-window-sensor-tuer-und-fensterkontakt-zigbee) at BerryBase
- [Twelve Zigbee sensors tested](https://smarthome-assistant.info/12-zigbee-sensoren-im-test-marktfuehrer-im-vergleich/)
- [Best Zigbee coordinators for Home Assistant 2026](https://smarthomescene.com/top-picks/best-zigbee-coordinators-for-home-assistant/)
- [SMLIGHT 2026 range guide](https://howmation.com/en_UK/blog/article/smlight-2026-range-guide-choosing-right-zigbee-thread-matter-coordinator)

Server and platform
- [Raspberry Pi 5 or N100 mini PC for Home Assistant, 2026](https://raspberry.tips/en/smart-home/home-assistant-hardware-2026-raspberry-pi-5-or-n100-mini-pc)
- [Home Assistant Green pricing change](https://www.nabucasa.com/news/2026-01-08-green-pricing-change/)
- [Home Assistant: deprecating the Core and Supervised installation methods](https://www.home-assistant.io/blog/2025/05/22/deprecating-core-and-supervised-installation-methods-and-32-bit-systems/)

Tenancy
- [Smart thermostats in a rented flat](https://www.drohnen.de/71799/smartes-heizkoerperthermostat-mietwohnung/)
