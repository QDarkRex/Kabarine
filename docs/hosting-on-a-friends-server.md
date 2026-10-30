# Asking a friend to host it

A message you can send (or adapt) when you want to run this on someone else's server. It
says exactly what the bot is and what it touches, so they can decide.

---

Hey, I'd like to run a small personal bot on your server next to the stuff you already host.
Here's exactly what it is and what it touches.

## What it does
It's a Discord bot that watches a list of **public** TikTok (and maybe Instagram) accounts
and copies their **new** posts and stories into our Discord server. It also downloads a link
when someone pastes it in chat. Personal use, not redistribution.

## How it runs on your box
- **One Docker container** (`tiktokbot`) with its own `docker-compose` file, in its own
  folder. It follows the same pattern as your other containers. Nothing is installed on the
  host itself.
- It runs as a **non-root user** (uid 1000) inside the container.
- It's **resource-capped**, so it can't hog the machine or starve your other containers:
  **512 MB** RAM, **2** CPU cores, **256** processes.
- `restart: unless-stopped` brings it back after a reboot, like your other services.

## Network behaviour
- **Outbound only.** It talks to TikTok, Instagram, X's public mirror API and Discord.
- **No published ports.** Nothing from the internet can reach it.
- It has its own Docker network and doesn't touch your other containers.

## Footprint
- **Disk:** tiny. It keeps one small SQLite file. Media is downloaded to a temp folder and
  **deleted right after** it's posted.
- **Bandwidth:** modest. A handful of video downloads a day; the "anything new?" checks are
  small.
- **CPU:** mostly idle. It only spikes briefly when a video is too big for Discord and has to
  be re-compressed.

## The IP question (the important one)
The bot reads TikTok like a logged-out visitor. The realistic worst case is that TikTok
**rate-limits** those requests for a while. That only stops **the bot** from reading TikTok.
It doesn't affect your other containers or your own TikTok use from your phone or home.

To stay polite, the bot:
- spreads its checks out (each account every few minutes, evenly paced, with jitter),
- **slows down automatically** if TikTok starts pushing back,
- polls Instagram much more slowly, through a throwaway account,
- and can route its traffic through a **proxy** if you'd rather your server's IP were never
  involved.

## Security
- It's unprivileged and resource-capped, with no inbound ports and an isolated network.
- Its secrets (a Discord token, plus an optional throwaway-account cookie file) live in a
  `chmod 600` file. They're never in the image and never in git.

## If you ever want it gone
One command, and nothing is left behind:

```bash
cd ~/tiktokbot && docker compose down        # stop + remove the container
rm -rf ~/tiktokbot                            # optional: wipe the files too
```

You can see what it's doing any time with `docker compose logs -f`, or in Portainer.

Totally fine if the answer's no. I just wanted you to have the full picture. 🙏
