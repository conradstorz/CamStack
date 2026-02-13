# CamStack Development Guide

## Live Development Mode

CamStack is designed to support live development directly from the Git repository. When you run the installer, it creates **symlinks** instead of copying files, allowing you to edit code in this repository and see changes immediately on the running system.

## How It Works

### Installation Creates Symlinks

When you run `install_me.sh`, the installer:

1. **Creates `/opt/camstack` as a symlink** to `CamStack_1.0.0/camstack/` in this repository
2. **Symlinks systemd service files** from `/etc/systemd/system/` back to the project tree

No files are duplicated outside the project tree. Everything runs from the canonical source location.

### File Locations

| System Location | Points To | Type |
|----------------|-----------|------|
| `/opt/camstack/` | `<repo>/CamStack_1.0.0/camstack/` | Symlink (directory) |
| `/etc/systemd/system/camstack.service` | `/opt/camstack/services/camstack.service` | Symlink (file) |
| `/etc/systemd/system/camplayer.service` | `/opt/camstack/services/camplayer.service` | Symlink (file) |
| `/etc/systemd/system/camredirect.service` | `/opt/camstack/services/camredirect.service` | Symlink (file) |

### Runtime Files Location

Even runtime files created by the application (logs, certs, configs) are stored within the project tree:

- `CamStack_1.0.0/camstack/runtime/` - Configuration, overlays, snapshots
- `CamStack_1.0.0/camstack/logs/` - Application logs
- `CamStack_1.0.0/camstack/certs/` - TLS certificates
- `CamStack_1.0.0/camstack/ca/` - Private CA files (if used)

## Development Workflow

### Editing Python Code

Python code changes take effect immediately:

1. Edit files in `CamStack_1.0.0/camstack/app/`
2. Restart the relevant service:
   ```bash
   sudo systemctl restart camstack.service    # For FastAPI web app
   sudo systemctl restart camplayer.service   # For mpv player
   sudo systemctl restart camredirect.service # For HTTP redirect
   ```

### Editing Systemd Service Files

Service file changes require a daemon reload:

1. Edit files in `CamStack_1.0.0/camstack/services/`
2. Reload and restart:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart <service-name>
   ```

### Editing Scripts

Scripts are executed from the symlinked location, so changes take effect immediately on next run:

1. Edit files in `CamStack_1.0.0/camstack/scripts/`
2. Run the script directly or via systemd

### Editing Templates

Template changes take effect on next page load (FastAPI auto-reloads in development):

1. Edit files in `CamStack_1.0.0/camstack/app/templates/`
2. Refresh browser (FastAPI development mode auto-reloads)

## Useful Commands

### Check Service Status
```bash
sudo systemctl status camstack.service
sudo systemctl status camplayer.service
sudo systemctl status camredirect.service
```

### View Logs
```bash
# Systemd logs
sudo journalctl -u camstack.service -f
sudo journalctl -u camplayer.service -f

# Application logs
tail -f /opt/camstack/logs/*.log
```

### Restart All Services
```bash
sudo systemctl restart camstack.service camplayer.service camredirect.service
```

### Verify Symlinks
```bash
ls -la /opt/camstack           # Should show -> to repository
ls -la /etc/systemd/system/cam*.service  # Should show -> to /opt/camstack/services/
```

## Installing Updates

After pulling changes from Git:

```bash
cd /home/pi/camstack-install
git pull

# If dependencies changed
cd /opt/camstack
uv sync

# If services changed
sudo systemctl daemon-reload

# Restart services
sudo systemctl restart camstack.service camplayer.service camredirect.service
```

## Benefits of Symlink Approach

1. **No File Duplication** - Single source of truth in Git repository
2. **Immediate Changes** - Edit code and restart service, no re-installation needed
3. **Version Control** - All changes including runtime configs can be committed
4. **Easy Rollback** - `git checkout` to revert changes
5. **Clean Development** - No confusion between "installed" and "development" versions

## Important Notes

⚠️ **Do not delete** `/opt/camstack` thinking it's a regular directory - it's a symlink to your repository!

⚠️ **Runtime files** (certs, logs, configs) are created inside the repository tree. Add them to `.gitignore` if they shouldn't be committed.

⚠️ **Python virtual environment** (`.venv`) is created in the repository at `CamStack_1.0.0/camstack/.venv` - this is intentional and allows you to use the venv for development.

## Reinstalling

If you need to reinstall (e.g., after major file structure changes):

```bash
cd /home/pi/camstack-install/CamStack_1.0.0
sudo bash install_me.sh
```

The installer will detect the existing symlink, back it up if needed, and create a fresh symlink.
