# Rivals Community

A community multiplayer launcher for the Windows Steam release of Need for Speed Rivals. Use Hamachi to host or join a two-player session after EA's multiplayer shutdown.

## Play

1. Extract the Windows x64 release ZIP on both PCs. Open **RivalsLauncher.exe** from the extracted folder.
2. Connect both PCs to the same Hamachi network. The host also needs Python 3.10 or later with python.exe on PATH.
3. The launcher looks for Rivals in your Steam libraries. If needed, choose **Change game folder** and select the folder that contains NFS14.exe.
4. The host selects **Host Session**, approves the Windows route prompt, and shares the address using **Copy Host IP**. The guest pastes the address into **Host's Hamachi IPv4** and selects **Join Session**.
5. Continue from the Rivals main menu and let the game search for the session. The host can lock or unlock admission from the launcher while playing.
6. Exit Rivals or select **End Session**. Hosting has no time limit. The launcher removes its temporary Windows route as the host session ends.

The profile panel saves a display name and photo locally. Rivals may still show its default photo in-game. Each player needs the supported Steam installation. The host service provides the Steam Complete Edition car-pack replies for installations the launcher can match to Steam.

If the guest cannot connect, check that both players are on the same Hamachi network and that the host firewall allows TCP 42127, 14219 and 17502 on Hamachi. Rivals uses UDP 3659 and 9999 for game traffic. There is no need to forward public router ports.

The launcher EXE contains its background, icon, and Rivals UI fonts. Keep the multiplayer bridge and reference-service folder beside the EXE to host or join.

## Build

Install Visual Studio 2022 with **Desktop development with C++**, a Windows SDK, and CMake 3.24 or later. From the source folder, run:

    .\build.ps1

The executable and runtime files are in **build\Release**. To produce source and Windows archives, run:

    python .\tools\package.py --version 1.1.4

Archives are written to **dist**. See [third-party notices](THIRD-PARTY-NOTICES.md) for upstream sources and licenses. Need for Speed Rivals is an EA game. This launcher is an independent community project.
