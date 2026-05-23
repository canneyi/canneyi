# Podcast tracker

Tracks 22 podcasts. Twice weekly (Mon + Thu) a scheduled Claude Code session pulls new episodes, writes detailed structured notes to `digests/`, commits them, and emails the digest.

## Files

- `podcasts.json` — the list of tracked podcasts (name + Apple Podcast ID, optional feed override / `paused: true`)
- `fetch.py` — fetches feeds, detects new episodes, writes `cache/pending_episodes.json`
- `AGENT_PLAYBOOK.md` — the instructions the scheduled session follows when summarizing
- `cache/feeds.json` — resolved RSS feed URLs (cached after first iTunes lookup)
- `cache/seen_episodes.json` — episode IDs already digested, keyed by Apple Podcast ID
- `cache/pending_episodes.json` — ephemeral, not committed
- `digests/YYYY-MM-DD.md` — committed output

## First-time setup

In a Claude Code session with outbound network enabled:

```bash
cd /home/user/facebook
pip install -r requirements.txt
python podcast_tracker/fetch.py --seed
git add podcast_tracker/cache/feeds.json podcast_tracker/cache/seen_episodes.json
git commit -m "Seed podcast tracker cache"
git push
```

`--seed` marks every episode currently published as already seen, so the first real run only picks up episodes published *after* the seed.

## Scheduled session setup

Configure a recurring trigger in the Claude Code web app
(<https://code.claude.com/docs/en/claude-code-on-the-web>):

- **Repository:** `canneyi/facebook`
- **Branch:** `claude/podcast-tracking-agent-SgDto`
- **Schedule:** Mondays and Thursdays, 14:00 UTC (or whatever time you prefer)
- **Network policy:** must allow outbound HTTPS to at least
  `itunes.apple.com` and the podcast hosting CDNs (Simplecast, Megaphone,
  Acast, Transistor, Libsyn, Substack, podcast website domains).
- **Prompt:** `Run the podcast tracker` — the session loads
  `podcast_tracker/AGENT_PLAYBOOK.md` from the repo and executes it.
- **Tools:** must include the Gmail MCP server (already attached to this
  account as `paddycake`'s Gmail) so the digest can be emailed to
  `vpst6tdbw2@privaterelay.appleid.com`.

## Adding or removing podcasts

Edit `podcasts.json` and commit:

```jsonc
{
  "name": "New Podcast Name",
  "apple_id": "1234567890"  // numeric ID from the podcasts.apple.com URL
}
```

Set `"paused": true` to skip a feed without removing it. Set
`"feed_url": "https://..."` to bypass the iTunes lookup when you already
know the RSS URL.

## Manual run

```bash
python podcast_tracker/fetch.py     # writes cache/pending_episodes.json
# then ask Claude to follow AGENT_PLAYBOOK.md against that file
```
