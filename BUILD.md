# Pi Manager — Cross-platform build notes

## Run from source

```bash
python -m pip install -r requirements.txt
python main.py
```

Requires:
- Python 3.10+
- Node.js + `npm install -g @earendil-works/pi-coding-agent`

## Platform notes

| OS | Terminal launch | Secrets |
|----|-----------------|---------|
| Windows | Windows Terminal / PowerShell / cmd | OS keyring + DPAPI/AES-GCM file vault fallback |
| macOS | Terminal.app / iTerm2 | Keychain via keyring + file vault |
| Linux | gnome-terminal / konsole / xterm / x-terminal-emulator | Secret Service via keyring + file vault |

## PyInstaller (examples)

### Windows
```bat
python -m PyInstaller --noconfirm --clean PiManager.spec
python -m PyInstaller --noconfirm --clean PiManagerOneFile.spec
```

`PiManager.spec` builds the recommended directory distribution. `PiManagerOneFile.spec`
builds the slower-starting single executable. Both include the Provider environment
helper and the keyring/cryptography modules required by the v1.6.0 credential flow.

### macOS
```bash
pyinstaller --noconfirm --windowed --name PiManager --paths . --collect-data certifi \
  --hidden-import keyring.backends --hidden-import keyring.backends.macOS \
  --hidden-import pi_manager.platform_util --hidden-import pi_manager.extras \
  --hidden-import pi_manager.secrets --hidden-import pi_manager.ui_features \
  --hidden-import pi_manager.help_docs main.py
# result: dist/PiManager.app
```

### Linux
```bash
pyinstaller --noconfirm --windowed --name PiManager --paths . --collect-data certifi \
  --hidden-import keyring.backends --hidden-import keyring.backends.SecretService \
  --hidden-import pi_manager.platform_util --hidden-import pi_manager.extras \
  --hidden-import pi_manager.secrets --hidden-import pi_manager.ui_features \
  --hidden-import pi_manager.help_docs main.py
# result: dist/PiManager/
```

GUI can be packaged on all three; full Pi sessions still require the official `pi` CLI on PATH.
