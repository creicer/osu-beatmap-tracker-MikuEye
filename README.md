# MikuEye – osu! Beatmap Status Tracker

MikuEye monitors osu! beatmaps and notifies you when their ranked status changes (e.g., Qualified → Ranked).  
Perfect for camping maps to grab top ranks.

![screenshot](screenshot.png)

## Features

- Track any beatmap by ID or search in the built‑in Browse tab
- Real‑time status updates (Graveyard → Pending → Qualified → Ranked/Loved)
- Sound notifications on status changes
- Full history log with filtering and export/import
- Modern dark UI with multi‑select and keyboard shortcuts

## Download

Grab the latest executable from the [Releases](https://github.com/your-username/osu-beatmap-tracker-MikuEye/releases) page.  
Just download `MikuEye.exe` and run it.

## How to use

1. Get your osu! API v2 credentials from [osu.ppy.sh/home/account/edit](https://osu.ppy.sh/home/account/edit) (OAuth section)
2. Enter them in **Settings**
3. Add beatmaps via the **Browse** tab (search by artist/title/mapper/ID)
4. Enable tracking on cards (click the ON/OFF badge or double click)
5. Press **START TRACKING**

See the in‑app **Info** dialog for more details and keyboard shortcuts.

## Building from source

If you prefer to run from source:

```bash
git clone https://github.com/your-username/osu-beatmap-tracker-MikuEye.git
cd osu-beatmap-tracker-MikuEye
pip install -r requirements.txt
python main.py
