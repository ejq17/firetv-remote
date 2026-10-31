# Fire TV Remote (free, no app)

A web remote for Amazon Fire TV / Fire Stick for when the physical remote goes missing.
Works from any browser, on your computer or your phone. No ads, no subscription, no ADB,
no developer mode: it talks to the same local pairing API the official Fire TV phone app uses.

**Live page:** https://ejq17.github.io/firetv-remote/

## How it works

Your Fire TV has a small HTTPS service on your home network (port 8080). A web page on
GitHub Pages cannot reach it directly (self-signed certificate, no CORS), so a tiny
Python script, `bridge.py`, runs on your computer and relays the button presses.

```
browser (this page)  ──►  bridge.py on your Mac  ──►  Fire TV on your Wi-Fi
```

The bridge uses only Python's standard library. Nothing to install.

## Setup (about two minutes)

1. Download [`bridge.py`](https://raw.githubusercontent.com/ejq17/firetv-remote/main/bridge.py)
   (or clone this repo).
2. In Terminal, run it:

   ```bash
   python3 bridge.py
   ```

   Leave that window open while you use the remote.
3. Open https://ejq17.github.io/firetv-remote/ (or http://localhost:8765, which the bridge
   also serves and which works in every browser including Safari).
4. Click **Find TVs** and pick your Fire TV, or type its IP address.
5. Click **Pair with TV**. A 4-digit PIN appears on the TV screen. Type it in and click **Verify**.

That's it. Pairing is remembered in `~/.firetv-remote.json`, so next time you just run the
bridge and go.

### From your phone

With the bridge running on your Mac, open `http://<your-mac-ip>:8765` on a phone on the same
Wi-Fi. The bridge prints that address when it starts.

## Buttons

Home, Menu, Back, Sleep, D-pad (hold an arrow to scroll fast), Select, Play/Pause, Rewind,
Fast-forward, Volume up/down, Mute, type text into the TV, and app shortcuts (Netflix, Prime,
YouTube, Disney+, Hulu, Max, Apple TV, Spotify, plus "load installed apps").

Keyboard shortcuts on a computer: arrows, Enter, Esc/Backspace, H, M, Space.

## Notes and limits

- Volume/mute keys depend on whether your Fire TV controls the TV volume (HDMI-CEC/IR).
  The navigation keys work on everything that supports the API.
- Confirmed by the community on Fire TV Stick 4K (1st gen and Max gen 2) and the Fire TV
  4K box. Very old Fire TVs may not have the service.
- The Fire TV must be awake and on the same Wi-Fi as the computer running the bridge. The
  bridge sends a wake-up request automatically when the TV isn't answering.
- Turn off any VPN on the Fire TV itself; it blocks the local API.
- Safari blocks an https page from talking to localhost. Use Chrome/Firefox for the GitHub
  Pages URL, or just open http://localhost:8765.

## Protocol credits

The API details come from community reverse-engineering:
[SLC-Josh/FireTVRest](https://github.com/SLC-Josh/FireTVRest),
[hms-homelab/hms-firetv](https://github.com/hms-homelab/hms-firetv),
[mase1981/uc-intg-firetv](https://github.com/mase1981/uc-intg-firetv) and the
[Home Assistant thread](https://community.home-assistant.io/t/rest-commands-for-fire-tv/901314).

## Testing without a TV

`dev/mock_firetv.py` fakes a Fire TV on localhost (PIN 1234):

```bash
python3 dev/mock_firetv.py
FIRETV_REMOTE_CONFIG=/tmp/ftv.json python3 bridge.py
```

with `/tmp/ftv.json` containing `{"ip":"127.0.0.1","firetv_port":18080,"dial_port":18009}`.

## License

MIT
