# Scheduling find-a-home to run every 6 hours

Three options depending on your environment:

---

## Option 1 — System cron (Linux / macOS)

Edit your crontab with `crontab -e` and add:

```cron
# Run find-a-home every 6 hours
0 */6 * * * cd /path/to/find-a-home && /path/to/venv/bin/python main.py run >> /var/log/find-a-home.log 2>&1
```

Find your Python path: `which python` (inside your virtual environment).

---

## Option 2 — Docker Compose (recommended for servers)

```bash
# Start the cron service (runs every 6 hours in a loop)
docker compose up -d cron

# View logs
docker compose logs -f cron
```

The `cron` service in `docker-compose.yml` uses a `sleep 21600` loop.
For a proper cron scheduler inside Docker, install
[supercronic](https://github.com/aptible/supercronic) and replace the
entrypoint with:

```yaml
command: ["supercronic", "/etc/cron.d/find-a-home"]
```

With a `crontab` file:
```cron
0 */6 * * * python /app/main.py run
```

---

## Option 3 — macOS launchd (native, no Docker required)

Create `~/Library/LaunchAgents/com.find-a-home.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.find-a-home</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/venv/bin/python</string>
    <string>/path/to/find-a-home/main.py</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/find-a-home</string>
  <key>StartInterval</key>
  <integer>21600</integer>
  <key>StandardOutPath</key>
  <string>/tmp/find-a-home.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/find-a-home.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- Add any env vars not in your .env file here -->
  </dict>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.find-a-home.plist
```

Unload it:
```bash
launchctl unload ~/Library/LaunchAgents/com.find-a-home.plist
```

---

## Option 4 — iOS / Mobile (future)

When the FastAPI layer is live, schedule calls to `POST /api/v1/search`
from a background fetch task in Swift (BGAppRefreshTask) or a serverless
cron (Vercel Cron, AWS EventBridge) that hits the deployed API endpoint.
