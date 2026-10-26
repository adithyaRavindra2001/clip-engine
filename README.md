# Clip engine

A clipping engine that runs on your own Mac. It finds fresh streams and podcasts from creators you have
permission to clip, picks the best moments with Claude, and renders vertical Shorts/Reels with captions.
It then queues them on a local review page. **You approve, tweak, and upload. Everything else is automatic.**

```
discover ─► audio download ─► hotspots ─► transcribe ─► Claude picks moments ─► download just those
(Twitch,    (audio only,      (viewer     (mlx-whisper,  (ranked, titled,         sections ─► render
 YouTube)    small)            clips,      only around    hook text, caption)      9:16 + captions + hook
                               replays,    hotspots on                                     │
                               loudness)   long VODs)                                      ▼
                                                                        review page (approve / reject /
          weekly: `clip learn` rewrites the style guide  ◄────────────  edit hook / trim) ─► ready/ folder
          from your decisions + real view counts                         you upload + log the post URL/views
```

## Permission is built in

`creators.yaml` is your permission registry. The engine **refuses to clip** anyone not marked
`status: allowed`. Mark a creator allowed only after you've joined their paid clipping campaign (Whop
Content Rewards, clipping.net, etc.) or they've said yes in writing. Paste the proof into `permission:`.
Everyone else shows up in the daily **prospects report** as someone to find a campaign for, or to pitch.

## Setup (Apple Silicon Mac, ~20 minutes)

```bash
git clone <this repo> clip-engine && cd clip-engine
./scripts/setup_mac.sh          # installs ffmpeg + Python deps, creates config files
```

Then fill in `.env`:

| Key | Where to get it | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys (add ~$5–10 credit) | ~$0.10–0.30 per hour of VOD analysed |
| `TWITCH_CLIENT_ID` / `SECRET` | dev.twitch.tv/console/apps → Register app (OAuth redirect: `http://localhost`) | free |
| `YOUTUBE_API_KEY` | console.cloud.google.com → enable *YouTube Data API v3* → Credentials → API key | free (10,000 units/day) |

Add creators to `creators.yaml`, then:

```bash
source .venv/bin/activate && source .env

clip run "https://www.twitch.tv/videos/123456789" --creator SomeStreamer   # one VOD
clip run ~/Downloads/raw_episode.mp4 --creator SomePodcast                  # a file the creator sent you
clip review                                                                 # open http://localhost:8765
```

When that works, automate it:

```bash
clip daily                      # one video a day: fixed campaign footage first (new moments each day), then fresh uploads
./scripts/daily_claude.sh       # same, but Claude Code (`claude -p`, your subscription) does the picking: no API key needed
./scripts/install_schedule.sh   # run daily_claude.sh every day at 17:30 and keep the review page alive
```

Campaigns that ship a fixed set of videos: list them under `sources:` in `creators.yaml` with the campaign's `cpm:`.
`clip daily` works the best-paying campaign first and cuts *new* moments from its least-used source, never
repeating a moment it has already cut.

## Daily routine (15–30 minutes)

1. `clip review`. Watch the drafts, which are sorted by Claude's score. Approve or reject them, and add a
   one-line reason. Reasons are how the engine learns your taste. Fix a weak hook or trim, then hit **Re-render**.
2. Approved clips land in `ready/<date>/` as `.mp4` + `.txt` (title, caption, required tags, credit, checklist).
3. Upload to YouTube Shorts and Instagram Reels, and submit the links to the campaign.
4. In the review page's **Approved** tab, paste the post URL. Fill in views/earnings when the campaign pays.
5. Weekly: run `clip learn`. Claude rewrites `data/style_guide.md` from what actually performed, and that
   guide steers future picks. `clip stats` shows performance per creator.

## Layouts

Each creator in `creators.yaml` has a `layout`:

- `auto` (default): finds the face. A small face in a corner is treated as a facecam, which gives
  **split** (facecam on top, gameplay below). Otherwise the frame is cropped around the face.
- `crop`: a single 9:16 crop centred on the speaker. Best for podcasts.
- `split`: forces split. Set `facecam: [x, y, w, h]` (fractions of the frame) if auto-detection misses it.

## Staying on the right side of the platforms

- **Never auto-upload.** YouTube locks API uploads from unaudited apps to private anyway. Mass automated
  uploading is also exactly what YouTube's spam policy targets.
- **Add something original** to every clip: the hook text, a sharp title, a good trim. YouTube's
  reused-content rule applies *even with the creator's permission*.
- **One channel per niche.** Don't post the same clip on several channels you run.
- **Keep clips under 60s**, and don't add copyrighted music.
- **Never buy views.** Campaigns detect it and withhold payment.

## Tuning

`config.yaml` controls:

- `clips.per_video`: candidates per VOD
- clip length limits
- `claude.effort`: `low` is cheaper, `high` is choosier
- `claude.mode`: `api` (default) calls Claude with your key. `file` needs no key: `clip run` stops after
  transcription, writes the full prompt to `data/videos/<id>/pick_request.md`, and waits for you (or a
  Claude Code session) to answer it as `picks.json` next to it. Rerun the same `clip run` to render.
  `auto` uses `file` only when `ANTHROPIC_API_KEY` is unset.
- the transcription model, plus `language: hi` for Hindi/Hinglish creators
- discovery limits

Kick has no reliable public VOD API, so queue Kick VODs with `clip run <kick vod url> --creator NAME`.
