# Clai TALOS .deb Deployment Fixes

## Issues Identified

After deploying the `.deb` package and configuring OTA updates, the service encounters two critical blocking issues:

### Issue 1: Missing Web Template Files
**Symptom:** `FileNotFoundError: [Errno 2] No such file or directory: '/opt/clai-talos/web/activity.html'`

**Root Cause:** 
- The `.deb` package contains web files at `/opt/clai-talos/src/web/` (part of the full source tree)
- The application's `app_paths.web_resource_dir()` resolves to `{resource_root}/web`, expecting files at `/opt/clai-talos/web/`
- This mismatch prevents the dashboard, login, and all other web templates from loading

**Impact:** 
- Dashboard (`:8080`) returns 500 errors
- All web-based features fail
- Service appears to run but is unusable

### Issue 2: SSL Certificate Path Not Set
**Symptom:** `FileNotFoundError: [Errno 2] No such file or directory` during httpx SSL context initialization

**Root Cause:**
- When httpx initializes for HTTPS requests (OTA metadata fetch, API calls), it tries to load CA certificates
- Without `SSL_CERT_FILE` environment variable set, Python's ssl module cannot locate the certificates
- Even though `ca-certificates` package provides the certs, the runtime path is not configured

**Impact:**
- OTA status check fails (cannot fetch GitHub API metadata)
- Any HTTPS requests from the service fail
- Service starts but cannot perform remote operations

---

## Solutions

### Automated Fix (Recommended)

Run the provided fix script with sudo:

```bash
sudo bash /opt/clai-talos/src/scripts/fix_deb_deployment.sh
```

This script will:
1. Copy web templates from `src/web/` to the root `web/` directory
2. Ensure `ca-certificates` package is installed
3. Add `SSL_CERT_FILE` environment variable to `.env`
4. Restart the service and verify all fixes
5. Display a summary of results

### Manual Fix (If Script Fails)

If you prefer to apply fixes manually or need troubleshooting:

#### Step 1: Copy Web Templates
```bash
sudo mkdir -p /opt/clai-talos/web
sudo cp /opt/clai-talos/src/web/*.html /opt/clai-talos/web/
sudo chown -R clai-talos:clai-talos /opt/clai-talos/web/
sudo chmod 0644 /opt/clai-talos/web/*.html
```

#### Step 2: Install ca-certificates
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates
```

#### Step 3: Configure SSL Certificate Path
```bash
# Check if already configured
grep SSL_CERT_FILE /var/lib/clai-talos/.env

# If not present, add it
sudo bash -c 'echo "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" >> /var/lib/clai-talos/.env'
```

#### Step 4: Restart Service
```bash
sudo systemctl restart clai-talos
sleep 3
sudo systemctl status clai-talos
```

#### Step 5: Verify
```bash
# Check recent logs
sudo journalctl -u clai-talos -n 50 --no-pager

# Test dashboard (replace with your server IP)
curl -I http://localhost:8080/
```

---

## Verification

After applying fixes, confirm everything works:

### 1. Service Status
```bash
sudo systemctl status clai-talos
```
Should show: `active (running)`

### 2. Web Templates
```bash
ls -la /opt/clai-talos/web/*.html
```
Should list: `activity.html`, `dashboard.html`, `login.html`, `settings.html`, etc.

### 3. SSL Configuration
```bash
grep SSL_CERT_FILE /var/lib/clai-talos/.env
```
Should show: `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`

### 4. Service Logs
```bash
sudo journalctl -u clai-talos -n 100 --no-pager | grep -i "error\|failed" | head -10
```
Should show no FileNotFoundError or SSL-related errors

### 5. Dashboard Access
Navigate to: `http://<server-ip>:8080`
- Login page should load
- Dashboard should render without errors
- Settings > Updates should show OTA status

---

## Future Prevention

The build script has been updated to automatically copy web files to the root level during `.deb` package creation. Future package builds (`src/scripts/build.deb.sh`) will include:

```bash
# Explicitly copy web files to root level
if [[ -d "$APP_DIR/src/web" ]]; then
  mkdir -p "$APP_DIR/web"
  cp -r "$APP_DIR/src/web/"* "$APP_DIR/web/"
  chmod -R 0644 "$APP_DIR/web"
fi
```

This ensures web templates are always at the correct location in newly built packages.

---

## Troubleshooting

### Service Won't Start
```bash
# Check what's blocking it
sudo journalctl -u clai-talos -n 50 --no-pager

# Try starting manually to see full error
sudo -u clai-talos /opt/clai-talos/venv/bin/python /opt/clai-talos/src/talos_entry.py
```

### Dashboard Still Shows Errors
```bash
# Verify web files copied
ls /opt/clai-talos/web/

# Check file permissions
ls -la /opt/clai-talos/web/activity.html

# Should be readable by clai-talos user
```

### OTA Status Still "Unavailable"
```bash
# Verify SSL_CERT_FILE is set
grep SSL_CERT_FILE /var/lib/clai-talos/.env

# Verify file exists
ls -la /etc/ssl/certs/ca-certificates.crt

# Test HTTPS connectivity
curl -v https://api.github.com 2>&1 | head -20
```

### Still Seeing Errors After Fix
Save the script output and logs:
```bash
sudo journalctl -u clai-talos -n 200 --no-pager > /tmp/clai-talos-logs.txt
cat /var/lib/clai-talos/.env > /tmp/clai-talos-env.txt
ls -la /opt/clai-talos/web/ > /tmp/clai-talos-web-ls.txt
```

Then review the files to identify any remaining issues.

---

## Related Files

- **Build Script:** `src/scripts/build.deb.sh` (updated to copy web files)
- **OTA System:** `src/ota_update.py` (handles version checking and updates)
- **Web Backend:** `src/telegram_bot.py` (serves templates via `render_template()`)
- **Path Resolution:** `src/app_paths.py` (defines `web_resource_dir()`)
