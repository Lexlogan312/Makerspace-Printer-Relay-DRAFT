# Raspberry Pi setup

This guide takes a brand-new Raspberry Pi 5 to a running relay:

```
 Bambu A1 printers ──Wi-Fi──▶ wlan0: "MakerspacePrinters" hotspot (10.42.0.0/24, no internet)
                                   │
                              Raspberry Pi 5 ── printer-relay.service
                                   │
 Supabase ◀── internet ── eth0 (Ethernet) or wlan1 (USB Wi-Fi) on the campus network
```

Do the steps in order. The Pi needs internet for steps 2–6, and step 7 turns the
built-in Wi-Fi into the printer hotspot. That's why the campus connection has to be
on a **second** interface (step 5) before you get there.

**Where:** steps 1–4 work anywhere with Wi-Fi, including at home. Steps 5–10 happen in the
makerspace, on the campus network and near the printers.

**No monitor needed.** Everything is done over SSH from your Mac. Have a fallback ready in case
a network change cuts you off:
- a **micro-HDMI** cable (the Pi 5 has micro-HDMI ports) plus a monitor and USB keyboard, or
- an **Ethernet cable** straight from your Mac (or a USB-C Ethernet adapter) to the Pi. Then
  `ssh <username>@makerspace-relay.local` works with no network at all.

Wherever "admin dashboard" appears below: until the dashboard is built, use Supabase's
**Table Editor** on the same tables instead.

## What you need

- Raspberry Pi 5, the official 27 W USB-C power supply, and a microSD card (16 GB or more)
- A campus connection that doesn't use the built-in Wi-Fi. Pick one:
  - **Ethernet** to a campus port (easiest and most reliable), or
  - a **USB Wi-Fi adapter** with Linux drivers, for campus Wi-Fi
- A computer with [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
- Your working `relay.toml` (Supabase URL and **secret** key `sb_secret_…`), and the database
  migrations already run and printers imported (see the README)

> **Check with ONU IT first.** Campus networks usually need headless devices
> registered by MAC address, and campus Wi-Fi is usually WPA2-Enterprise (username and password)
> or has a sign-in page, which a headless Pi can't get through. Ask which network and
> registration process to use for an always-on lab device. Get the Pi's MAC addresses with
> `ip link` after step 2.

---

## 1. Flash the SD card

1. Open Raspberry Pi Imager → **Device:** Raspberry Pi 5 → **OS:** *Raspberry Pi OS (other) →
   Raspberry Pi OS Lite (64-bit)* → **Storage:** your SD card.
2. Fill in the customisation settings. Imager 2 walks you through them; older versions show an
   **Edit settings** button:
   - Hostname: `makerspace-relay`
   - Username and password: pick your own and write them down (they go in the handoff notes)
   - Wireless LAN: your home Wi-Fi (or your phone's hotspot), so you can do steps 2–4 without
     Ethernet. Set the **Wireless LAN country to US** either way.
   - Locale: time zone `America/New_York`
   - **Services** tab: enable SSH (a public key is better; a password works)
3. Write the card, put it in the Pi, connect Ethernet if you have it, and power on.
   The first boot takes a couple of minutes.

## 2. Log in and update

From your Mac, on the same network as the Pi:

```bash
ssh <username>@makerspace-relay.local
```

If `.local` doesn't resolve (common on campus networks), find the Pi's IP from your
router, or plug in a monitor and run `hostname -I`.

On the Pi:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git nftables dnsmasq-base
sudo raspi-config nonint do_wifi_country US
sudo reboot
```

### Recommended: Raspberry Pi Connect (remote access from anywhere)

A backup way in that works even if the campus network blocks SSH between devices, since
the Pi connects out to Raspberry Pi's servers. It needs a free Raspberry Pi ID. Use one the
makerspace keeps (e.g. a shared makerspace email), with a strong password and two-factor login,
because that account can open a terminal on the Pi. Skip this if you enabled Connect in Imager.

```bash
sudo apt install -y rpi-connect-lite
rpi-connect on
loginctl enable-linger      # keep it running when nobody is logged in
rpi-connect signin          # open the printed link on your Mac and approve the Pi
```

To use it: go to connect.raspberrypi.com → your Pi → **Remote shell**.

## 3. Get the code onto the Pi

**If the repo is on GitHub:**

```bash
git clone https://github.com/<you>/<repo>.git ~/printer-relay
```

**Otherwise, copy it from your Mac.** Run this in the project folder on the Mac:

```bash
rsync -av --exclude .venv --exclude raw --exclude .idea --exclude __pycache__ --exclude relay.toml --exclude printers.toml ./ <username>@makerspace-relay.local:printer-relay/
```

Then copy the config separately. It's gitignored because it holds the Supabase secret key:

```bash
scp relay.toml <username>@makerspace-relay.local:printer-relay/
```

On the Pi, make it readable only by you:

```bash
chmod 600 ~/printer-relay/relay.toml
```

Then edit it (`nano ~/printer-relay/relay.toml`) and set the printer network to the hotspot
you'll create in step 7. Discovery then ignores any Bambu printers elsewhere on campus:

```toml
printer_network = "10.42.0.0/24"
```

## 4. Install Python dependencies

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
cd ~/printer-relay
uv sync
uv run pytest
```

`uv sync` downloads Python 3.13 if the OS has an older version, then creates `.venv`.
All tests should pass.

## 5. Set up the campus (internet) connection

Run `nmcli device` to see the interfaces. `wlan0` is the built-in Wi-Fi, which will become the
hotspot. The campus connection must be `eth0` or a USB adapter (`wlan1`).

**Ethernet:** nothing to do. NetworkManager uses it automatically once the port is registered.

**USB Wi-Fi adapter:** plug it in and confirm it shows up as `wlan1` in `nmcli device`. Then
add the campus network. For a WPA2-Enterprise network (get the real settings from ONU IT):

```bash
sudo nmcli con add type wifi ifname wlan1 con-name campus ssid "<CAMPUS_SSID>" \
  wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 \
  802-1x.identity "<username>" 802-1x.password "<password>"
sudo nmcli con up campus
```

For a normal password network, use this instead:

```bash
sudo nmcli dev wifi connect "<SSID>" password "<password>" ifname wlan1
```

Check that it works:

```bash
ping -c 3 supabase.com
timedatectl
```

`timedatectl` should say **System clock synchronized: yes**. The relay's timestamps and the
TLS connection to Supabase both depend on the correct time. If it says no, ask IT for an
NTP server and set it in `/etc/systemd/timesyncd.conf`.

If you set up Wi-Fi in Imager (step 1), remove it now so `wlan0` is free for the hotspot.
Current Raspberry Pi OS images apply Imager's Wi-Fi through cloud-init and netplan, which
recreate it on every boot. So turn cloud-init off (first-boot setup is finished) and take the
Wi-Fi out of the netplan file.

> **Don't cut off your own connection.** If you're SSH'd in over that Wi-Fi, deleting it
> disconnects you. First get the Pi's campus IP with `ip -4 addr show eth0` (or `wlan1`), check
> you can `ssh <username>@<that IP>` from your Mac on the campus network, and run the delete
> from that session. Write the IP down: `.local` names often don't work on campus networks.
>
> Some campus networks also block devices from connecting to each other. If you can't reach
> the Pi from your Mac on campus, use the Ethernet-cable fallback above, and ask IT whether
> the Pi's network allows SSH from your laptop for later maintenance.

```bash
sudo touch /etc/cloud/cloud-init.disabled     # stop first-boot setup from re-applying anything
ls /etc/netplan/                              # usually 50-cloud-init.yaml
sudo nano /etc/netplan/50-cloud-init.yaml     # delete the whole `wifis:` section, keep `ethernets:`
sudo netplan apply
nmcli con show                                # no wlan0 profiles should be left
```

On older images the Wi-Fi is a plain NetworkManager profile instead: `sudo nmcli con delete preconfigured`.

## 6. Choose a hotspot channel

The A1 only supports **2.4 GHz**. Before `wlan0` becomes a hotspot, see which channels are busy:

```bash
nmcli -f SSID,CHAN,SIGNAL dev wifi list ifname wlan0
```

Pick whichever of **1, 6 or 11** has the fewest strong networks.

## 7. Create the printer hotspot and the isolation firewall

The firewall goes in first, so the printers are never on the hotspot without it.

```bash
cd ~/printer-relay
sudo cp deploy/printer-isolation.nft /etc/printer-isolation.nft
sudo cp deploy/printer-isolation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now printer-isolation.service
sudo nft list table inet printer_isolation
```

Now create the hotspot. Choose a long password; the printers will store it.

```bash
sudo nmcli con add type wifi ifname wlan0 con-name printer-ap autoconnect yes \
  ssid MakerspacePrinters \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.pmf disable wifi-sec.psk "<HOTSPOT_PASSWORD>"
sudo nmcli con up printer-ap
```

Replace `6` with the channel you picked in step 6.

**Verify the isolation:** join `MakerspacePrinters` from your phone. You should get a `10.42.0.x`
address and **no internet**. SSH to `10.42.0.1` from that network should also fail.
Disconnect the phone afterwards.

## 8. Move the printers onto the hotspot

On **each** printer's touchscreen:

1. **Settings → WLAN**: join `MakerspacePrinters` with the hotspot password.
2. Turn on **LAN Only Mode**. The printer has no internet here, so this stops it trying to
   reach Bambu's cloud.
3. Note the **Access Code** shown in the LAN settings. If it's different from the one in
   Supabase, update it in the admin dashboard (`printer_connections.access_code`).

**IP addresses are found automatically.** Each printer announces its IP on the hotspot every
~10 seconds, and the relay connects to it and saves it to `printer_connections.host`. When a
printer gets a new IP, the relay reconnects within about 10 seconds. A printer that isn't in
Supabase yet shows up in the `discovered_printers` table, ready to add from the admin dashboard.

> If a printer's `last_error` says the access code was rejected even though it's right, the
> printer's firmware may be restricting LAN access. Turn on **Developer Mode** (next to LAN Only
> Mode in the printer's settings). The makerspace's A1s on firmware 01.08 haven't needed this.

### Optional: fixed IPs

Not required, but it gives each printer a predictable IP, which helps when troubleshooting. Once the
printers have joined, list them:

```bash
cat /var/lib/NetworkManager/dnsmasq-wlan0.leases
```

Each line shows an expiry time, MAC address, IP and hostname. Create
`/etc/NetworkManager/dnsmasq-shared.d/printers.conf` with one line per printer. Using the
printer number as the last part of the IP makes them easy to find:

```bash
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo nano /etc/NetworkManager/dnsmasq-shared.d/printers.conf
```

The file looks like this, with your printers' MACs:

```
dhcp-host=ac:a7:04:f6:b7:9c,10.42.0.101,FredPrintstone
dhcp-host=90:70:69:3b:8d:1c,10.42.0.102,FlintLockwood
```

Apply it:

```bash
sudo nmcli con down printer-ap && sudo nmcli con up printer-ap
```

The printers reconnect within a minute or so; power-cycle any that don't. The relay picks
up the new IPs from their announcements on its own.

## 9. Test, then install the service

Run it by hand first, as a dry run. It reads the printer list from Supabase and prints each
printer's status, but doesn't write anything:

```bash
cd ~/printer-relay
uv run python -m relay --dry-run
```

Every printer should show a status line within ~15 seconds. Press Ctrl+C. Then run it for real:

```bash
uv run python -m relay
```

In the Supabase Table Editor, `printer_status` rows should update, `printer_connections.host`
should show the `10.42.0.x` IPs, and a `relay_heartbeats` row should appear each minute. Press Ctrl+C.

Install it as a service, so it starts on boot and restarts itself if it crashes:

```bash
sed "s/__USER__/$USER/g" deploy/printer-relay.service | sudo tee /etc/systemd/system/printer-relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now printer-relay.service
systemctl status printer-relay
journalctl -u printer-relay -f
```

The last command follows the live logs; press Ctrl+C to stop watching.

## 10. Reboot test

```bash
sudo reboot
```

After the reboot, SSH back in over the campus network and check:

```bash
systemctl status printer-relay printer-isolation
nmcli con show --active
```

Both services should be active, and `printer-ap` plus the campus connection should both be
listed. Also confirm the dashboard data is updating.

Optional but recommended: turn on automatic security updates with
`sudo apt install -y unattended-upgrades`.

---

## Day-to-day

| Task | Command |
|---|---|
| Live logs | `journalctl -u printer-relay -f` |
| Restart after editing `relay.toml` | `sudo systemctl restart printer-relay` |
| Update the code (git) | `cd ~/printer-relay && git pull && uv sync && sudo systemctl restart printer-relay` |
| See which printers are on the hotspot | `cat /var/lib/NetworkManager/dnsmasq-wlan0.leases` |
| Add a printer | Join it to the hotspot (step 8). It appears in `discovered_printers`. Add it with its label and access code in the admin dashboard. The relay connects within 30 s, no restart needed |
| Remove a printer | Set its `maintenance_status` to `offline_permanent` in the dashboard. This keeps its history (deleting the row erases it) |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Printer shows `offline` | Check `printer_connections.last_error` in Supabase. It says whether the printer isn't on the network or its access code was rejected |
| `last_error`: "Not seen on the network" | Printer is off or not joined to `MakerspacePrinters` |
| `last_error`: "Access code rejected" | Fix the access code in the dashboard, or the printer needs Developer Mode (step 8) |
| Log: `has no access code in printer_connections, skipping` | The printer was added without an access code. Add it in the dashboard |
| Log: `unknown model code` | A new printer model. Add its code to `MODEL_NAMES` in `relay/discovery.py` |
| `Supabase … failed (401)` | Wrong key type. The relay needs the `sb_secret_` key, not `sb_publishable_` |
| `Supabase … failed` with a connection error | Campus uplink is down. Try `ping supabase.com` and `nmcli device` |
| Timestamps are wrong or TLS errors | Clock not synced. Check `timedatectl` (step 5) |
| Printers can't see `MakerspacePrinters` | Hotspot must be 2.4 GHz (`band bg`). Try another channel |
