# VIDAA Remote for macOS

A native SwiftUI interface for the same local TV protocol used by the GNOME app.
It supports TV discovery, independent pairing, navigation, volume, channels,
playback, input selection, and launching apps returned by the TV. The shared
Python backend handles MQTT, TLS pinning, and token reconnection.

## Build and install

Requirements: macOS 13 or newer, Xcode Command Line Tools, and a Python 3.9 or
newer interpreter with SSL support. Apple Silicon and Intel build targets are
selected from the build machine. The app uses the selected Python interpreter
at runtime, so keep that interpreter installed.

```sh
xcode-select --install
git clone https://github.com/ijaz-miks/vidaa-remote-gnome.git
cd vidaa-remote-gnome
python3 macos/install.py
open "$HOME/Applications/VIDAA Remote.app"
```

The installer builds an app under `~/Applications`, creates its icon, checks the
Paho 2.1.0 wheel checksum, retains dependency licenses, and signs the bundle
locally. It preserves existing bundles. Use `--destination /path/to/new.app`
for another installation and `--wheel /path/to/paho_mqtt-2.1.0-py3-none-any.whl`
for an offline dependency. `--python /path/to/python3` selects the runtime.
This is a local build, without Developer ID signing or notarization.

## Certificate and pairing

Supply the certificate from your own official VIDAA Android app, following
the main README's certificate instructions. The repository supplies no client
certificate, private key, APK, or TV authorization.

To import an extracted combined certificate and private-key PEM, choose
**TV > Import certificate…**, or use:

```sh
python3 tools/import_certificate.py --pem /path/to/your/client.pem
```

On macOS, the importer writes the PEM with mode `0600` under
`~/Library/Application Support/VIDAA Remote/`. It preserves an existing file.
Import from an APK or an authorized USB phone is also available through the
importer. OpenSSL 3 and the system LibreSSL command use their appropriate
PKCS12 options.

1. Allow **VIDAA Remote** to use the local network when macOS asks.
2. Choose **Find TV** or enter the TV's local address and select **Connect**.
3. Select **Pair this Mac with TV** and enter the current TV code.
4. The Mac saves its own pairing in Keychain, reconnects with the issued token,
   and enables controls once the TV accepts its control subscriptions.

Keep the connection open while entering the code. Restarting can invalidate it.
macOS 15 and newer manage app access under **System Settings > Privacy & Security
> Local Network**. A denied connection can report “No route to host” while SSH
still works. A new local build can require permission again because its signing
identity changes. Apple documents these rules in
[Understanding local network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy).

## Local data and automation

Mac settings, a newly generated client identity, the private PEM, and a small
health summary stay in the app's mode `0700` Application Support directory.
TV pairing tokens are stored in macOS Keychain under service
`dev.local.VidaaRemoteMac`, separately for each TV. The native app uses Apple's
Security framework; secret values pass to the child backend only through
anonymous pipes. They are never written to the health summary or logs.

Remote controls remain disabled until the TV authorizes this Mac. Source IDs
and app launch payloads come from the TV. The app checks its saved TLS pin before
sending MQTT credentials. It does not replay mutating commands after a failure.

For explicit automation within your own Mac account:

```sh
open "$HOME/Applications/VIDAA Remote.app" --args --automation
"$HOME/Applications/VIDAA Remote.app/Contents/MacOS/VidaaRemote" \
  --command '{"method":"refresh"}'
```

Automation uses a user-owned FIFO with mode `0600` inside the private settings
directory. It accepts control actions only. Keychain replies can only arrive
through the anonymous parent pipe. The FIFO contains no persistent command data
and has no network listener. It is enabled only when the app starts with
`--automation`. `status.json` exposes connection state and item counts without
tokens or device addresses.

## Demo preview and checks

```sh
open -n "$HOME/Applications/VIDAA Remote.app" --args --preview --quit-after 4
```

Preview uses fictional TV data and does not access the TV, real settings, or
Keychain. Add `--screenshot /path/to/new-demo.png` to capture the preview window.
The destination must not exist. Preview capture depends on macOS screen capture
permission and can be unavailable over SSH. The shared test suite covers token isolation,
TLS pinning, pairing reconnection, and the private Keychain bridge. macOS CI
builds and verifies the native bundle and runs the backend and bridge checks.

The GNOME USB phone interface is available on Linux. The macOS interface uses
direct TV control. TV and firmware differences can affect individual keys,
Wake-on-LAN, and app launching.
