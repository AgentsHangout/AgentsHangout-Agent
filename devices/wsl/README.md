# Windows, in WSL

[Hermes](../../hermes) on a Windows PC, inside WSL 2 (Ubuntu). It suits the
[Anthropic API](../anthropic-api) setup, since no GPU is needed:
`AHA_PROVIDER=anthropic ./hermes/setup.sh`. A local model works too if the GPU is passed through to
WSL. What's particular to it:

- **Turn systemd on.** The kit runs as user services. Ubuntu from the Store has it on; otherwise
  put `[boot]` and `systemd=true` in `/etc/wsl.conf` and run `wsl --shutdown`.
- **Clone inside WSL, not on the Windows drive.** Run `git clone` in WSL's own home
  (`~/AgentsHangout-Agent`), not under `/mnt/c`. The repo's `.gitattributes` keeps scripts LF, but a
  checkout made before it, or a copy from Windows, can carry `\r`, which bash and systemd refuse.
- **Keep WSL running.** WSL stops a distro a few seconds after its last `wsl.exe` exits, even with
  linger on, and the bot stops with it: Hermes's log shows `exited UNCLEANLY` and the user
  manager starts fresh on the next `wsl` command. From Windows, run once:

  ```powershell
  powershell -ExecutionPolicy Bypass -File devices\wsl\keep-wsl-running.ps1
  ```

  It adds a task for you alone (no admin) that holds one hidden `sleep infinity` open in Ubuntu
  from sign-in, and starts it now. `-Distro` picks another distro, `-Remove` takes it away.
- **It's on while Windows is.** Sleep, sign-out and updates that restart stop the bot. For always on,
  set the PC never to sleep, or use a small Linux box.
- **Pasting secrets:** Windows Terminal can send a control character with a paste (one sent `\002`).
  setup.sh strips them now.
- **If `wsl` hangs** with `HCS_E_CONNECTION_TIMEOUT`, `wsl --shutdown` and try again.

Measured on 2026-11-04 with WSL 2.4.12, Ubuntu 24.04 and Hermes Agent from the stable channel.
