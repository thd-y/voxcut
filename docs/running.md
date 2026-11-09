English | [Русский](running.ru.md)

# Installing and running VoxCut

VoxCut has a Linux command line and a Windows interface. Processing stays local. The interface runs in WSL2 and opens Microsoft Edge; it does not require a native Windows Python installation.

- [Ubuntu: command line](#ubuntu-command-line)
- [WSL: prepare once](#wsl-prepare-once)
- [Windows: clone and double-click](#windows-clone-and-double-click)

## Prerequisites

- **Ubuntu CLI:** Ubuntu 24.04 or another Linux with Python 3.12+, `make`, ffmpeg and libsndfile.
- **Windows interface:** Windows 11, WSL2 with Ubuntu 24.04 and systemd enabled, plus Microsoft Edge.
- A writable product folder on a local Windows drive or in WSL. Code, `.venv`, `inbox/`, `data/`, `backups/` and job records stay there. Network shares and cloud-synced folders are not supported locations.
- Internet access for the first dependency installation. Keep your proxy enabled if you use one. Normal operation uses the installed model without downloads.

## Ubuntu: command line

In an Ubuntu terminal, install system packages once:

```sh
sudo apt update
sudo apt install python3 python3.12-venv make git ffmpeg libsndfile1
```

Clone or unpack VoxCut into a writable folder, open a terminal there, then run:

```sh
make setup
.venv/bin/python -m voxcut.cli --help
```

`make setup` installs the pinned CPU build of torch. The browser interface requires Windows and WSL; native Ubuntu supports the CLI.

## WSL: prepare once

If WSL is absent, run in an elevated PowerShell and complete Ubuntu's first-run setup:

```powershell
wsl --install -d Ubuntu-24.04
```

Check the installation in PowerShell:

```powershell
wsl -l -v
```

Ubuntu must use version **2**. For a Windows-drive product, VoxCut uses the default distribution (marked `*`). If necessary, select Ubuntu:

```powershell
wsl --set-default Ubuntu-24.04
```

In Ubuntu, install the system packages listed above. Check systemd:

```sh
systemctl --user is-system-running
```

`running` or `degraded` means the manager is available. If it cannot connect, ensure `/etc/wsl.conf` contains the following, preserving its other settings:

```ini
[boot]
systemd=true
```

Then close active WSL work and run `wsl --shutdown` in PowerShell; this stops **all** WSL distributions. Reopen Ubuntu.

For a product stored in WSL, run from its folder:

```sh
python3 -m voxcut.bootstrap
```

This prepares an absent environment and opens the interface. After `make setup`, `make ui` also works. Double-clicking `VoxCut.cmd` through `\\wsl.localhost\<Distribution>\...` selects that path's distribution automatically.

## Windows: clone and double-click

1. Clone or unpack VoxCut into a writable local folder, for example `C:\Apps\VoxCut` or `D:\Audio tools\VoxCut`. Spaces, Cyrillic and square brackets in the path are supported.
2. Double-click **VoxCut.cmd**. The first launch prepares `.venv` inside that folder; allow the installation to finish. Missing system packages produce an instruction instead of an automatic privileged installation.
3. Keep the terminal open while using the Edge window. Ctrl+C stops the interface; jobs already started continue under systemd. A second launch opens another window for the same product.
4. Put WAV files into `inbox/<session folder>/`, then choose Import and Process in the Library.

No scripts need editing for the folder name or Windows username. Different product folders have separate data and UI instances. Only temporary UI coordination files live in a private WSL runtime directory; they contain no recordings, notes or processing results.

Before updating or moving the product, stop its interface and wait for jobs to finish. Move the **whole** folder, including hidden contents, then double-click again. The launcher refreshes an environment whose recorded location has changed. Moving between machines still requires preparing WSL and its system packages on the destination.

## Proxy: keep it enabled

VoxCut does not disable v2rayN, system proxy or TUN. Dependency installation inherits WSL's proxy environment; it does not retry by clearing proxy settings. Heavy processing jobs remain offline and isolated from the network.

For a Windows system proxy, ensure the existing `%USERPROFILE%\.wslconfig` includes these settings, preserving other entries:

```ini
[wsl2]
networkingMode=mirrored
autoProxy=true
```

Apply changes with `wsl --shutdown` after closing other WSL work. Microsoft documents [mirrored networking and proxy inheritance](https://learn.microsoft.com/en-us/windows/wsl/networking). If WSL does not inherit your proxy, configure `HTTP_PROXY` and `HTTPS_PROXY` in Ubuntu using the HTTP proxy endpoint shown in your proxy application's settings. Do not guess its port or publish URLs containing credentials. A TUN configuration can route traffic without these environment variables.

The interface connects to **127.0.0.1**, on the same machine. If the server starts but Edge cannot open it, check that local loopback traffic is allowed by your proxy/TUN rules. No LAN listener or firewall exception is needed.

## If launch fails

Keep the proxy on. In PowerShell, open the product folder and retain the error window:

```powershell
Set-Location -LiteralPath 'C:\Apps\VoxCut'
cmd /k .\VoxCut.cmd
```

- **No distribution / default distribution is unsuitable:** check `wsl -l -v` and select the prepared Ubuntu distribution.
- **Missing prerequisites / Python too old:** install the Ubuntu packages above; use Ubuntu 24.04 with Python 3.12+.
- **systemd unavailable:** follow the WSL preparation section.
- **Dependency installation failed:** check WSL's proxy connectivity and rerun the launcher; do not disable the proxy or delete `data/`.
- **Unsafe runtime directory:** do not loosen its permissions; stop the other instance and inspect the reported directory's ownership and links.

For environment checks while the interface is running, run `make ui-envcheck` from the product folder in Ubuntu/WSL. Before upgrading from an older release, close its UI first: the temporary coordination location has changed.
