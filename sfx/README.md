# Sound effects

Real samples from Mixkit (https://mixkit.co/free-sound-effects/), all under the Mixkit Sound Effects Free
License: free for commercial use including monetised YouTube/TikTok/Instagram, no attribution required,
may not be redistributed as standalone files. File names keep the Mixkit item id so the source is traceable.
Each file was trimmed of leading silence, capped at ~1 s with a short fade, and peak-normalised to -1 dBFS.

    whoosh/   plays as the hook text pops in (t = 0)
    impact/   plays on each punch-in zoom (the beats Claude marks); needs brand.punch_in and brand.sfx
    pop/      spare, unused for now

If a folder is empty, clipengine/sfx.py synthesises a fallback with ffmpeg. Audio files are gitignored.
