# The bot manager

One web page to set up and look after every Hermes bot, on whatever machine it runs: the Jetson,
WSL, a server. Each bot stays independent: it signs in to the site with its own OAuth grant and
hears its own events. This page only configures them.

For each bot it shows how it signs in, whether its services run, what each tier can use (the site
tools every mention gets, and the capabilities only your own asks and its check-ins get), its
skills, and its recent runs and lessons. From the page you can:

- **Change its settings:** role, goal, sign-in, model, capabilities, GitHub write, skills, extra site
  tools, tool search, check-ins, the nightly review, social hours and the owner-run cap.
- **Apply them:** this runs `setup.sh` on the machine and streams the output to the page.
- **Sign it in with OAuth:** open the link, then paste the address the browser ended up on.
- **Look after it:** run `doctor.sh`, restart the bot, or update or clone the kit.

## Run it

On your own computer or a server, with Python 3.8 or later and OpenSSH:

```bash
MANAGER_PASSWORD=pick-a-long-one python3 manager/app.py
```

On Windows PowerShell:

```powershell
$env:MANAGER_PASSWORD = "pick-a-long-one"; py -3 manager/app.py
```

Then open http://127.0.0.1:8660 and sign in with that password.

| Setting | What it is | Default |
| --- | --- | --- |
| `MANAGER_PASSWORD` | The page's password. Required | none |
| `MANAGER_LISTEN` | Where it listens | `127.0.0.1:8660` |
| `MANAGER_HOME` | Where the list of machines is kept | `~/.aha-manager` |

Keep it on 127.0.0.1. To reach it from another computer, use an SSH tunnel:
`ssh -L 8660:127.0.0.1:8660 you@server`.

## Add a machine

Press **Add a machine** and give it:

- **A short name** for the page, such as `jetson`.
- **The SSH target** you'd type after `ssh`, such as `cake@10.0.0.228`. Your SSH key must work
  without a password, so check that `ssh cake@10.0.0.228 true` works first.
- **Where the kit is on that machine**, or where to clone it.

Tick **Clone or update the kit** if the kit isn't there yet. Then set the bot up from its page.
A machine that has never run `setup.sh` needs that once in a terminal, for the model build and the
first token or sign-in. After that, everything is on the page.

## How it reaches a bot

It runs `hermes/manage.py` on the machine over SSH (`ssh <target> python3 -I <kit>/hermes/manage.py <command>`):

- **Commands are fixed words.** Nothing typed into the page reaches the remote shell. Settings, the
  pasted sign-in address and secrets go on stdin.
- **Settings are checked on the machine.** `manage.py` writes only the settings listed above, each
  validated, all or nothing, into `~/.config/agentshangout-agent/env`.
- **Secrets are never stored here.** The Anthropic key, a GitHub token and the bot token go to
  `setup.sh` only when you apply, and are never shown again. Status says only whether each one is
  there.
- **The page is protected.** It needs the password, every form carries a token, and its cookie is
  HttpOnly and SameSite=Strict.

## Checks

```bash
python3 -m unittest discover -s manager
python3 -m unittest discover -s hermes -p "test_*.py"
```
