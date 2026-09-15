# Canvas Nightly Agent

A small, read-only Canvas briefing. It checks incomplete planner items due in the next seven days, writes `reports/latest.md`, and shows a Mac notification. Scheduled runs do not call an AI model.

## Setup

1. Open `.env` in a local editor.
2. Set `CANVAS_BASE_URL` to the HTTPS origin you normally use for Canvas, with no path. Example: `https://canvas.example.edu`.
3. Paste your Canvas access token after `CANVAS_TOKEN=`. Never paste it into chat or a command.
4. Protect and validate the file:

   ```sh
   chmod 600 .env
   /usr/bin/python3 canvas_agent.py --check-config
   ```

## Test and run

```sh
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 canvas_agent.py --dry-run --no-notify
/usr/bin/python3 canvas_agent.py --test-notification
/usr/bin/python3 canvas_agent.py
```

The live run reads only `/api/v1/planner/items`. The client refuses non-GET methods, other hosts, insecure URLs, non-allowlisted paths, and tokens in URLs.

## Twice-daily schedule

The supplied `com.mkt382.canvas-briefing.plist` uses the Mac's local time and runs at 7:00 a.m. and 9:00 p.m. Because macOS blocks background jobs from reading `Documents`, the installed runtime and its protected `.env` live in `~/Library/Application Support/MKT382CanvasAgent`. The project `.env` is a link to that single credential file. Install it only after a successful live run:

```sh
mkdir -p "$HOME/Library/Application Support/MKT382CanvasAgent/logs" "$HOME/Library/Application Support/MKT382CanvasAgent/reports"
cp canvas_agent.py "$HOME/Library/Application Support/MKT382CanvasAgent/"
cp com.mkt382.canvas-briefing.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.mkt382.canvas-briefing.plist"
launchctl print "gui/$(id -u)/com.mkt382.canvas-briefing"
```

To uninstall:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.mkt382.canvas-briefing.plist"
mv "$HOME/Library/LaunchAgents/com.mkt382.canvas-briefing.plist" "$HOME/.Trash/"
```

The Mac must be logged in for notifications to appear. Inspect `logs/agent.log` and `reports/latest.md` after the first unattended run.
