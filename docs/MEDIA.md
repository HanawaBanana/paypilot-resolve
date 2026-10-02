# How the demo video was made

Video: <https://youtu.be/Rv2cLNXboPw> (2:20, public). A rendered copy lives in
`docs/media/paypilot_resolve_demo.mp4`; the thumbnail used on Devpost is
`docs/media/thumbnail.jpg`.

## Recording approach

Screen recorders were not usable for this project: the automation browser's task
window is off screen, so `screencapture` records whatever other window happens to
be in front. Instead the driver script that performs the demo **also captures
frames**: it calls `page.screenshot()` roughly ten times a second during every
wait and records the real timestamp of each frame. The frames are then encoded
with ffmpeg's concat demuxer using per-frame durations, so the video timeline is
the real one and narration can be aligned to measured scene starts.

Result: 546 frames over 137.9 seconds; encoded to 1920x1080 H.264 at 30 fps.

## Narration

`tools/narration.py` generates the fifteen narration segments with **edge-tts**
(free, no API key) using **`en-US-AvaNeural`** at `+20%` rate, then they are
mixed onto the video with `adelay` at the measured scene starts:

```
edge-tts -> n*.mp3
ffmpeg -i video_silent.mp4 -i narration.m4a -c:v copy -c:a copy demo.mp4
```

## Scene starts (seconds from the first frame)

| scene | starts | shows |
|---|---|---|
| 0 | 0.0 | console at rest: sandbox mode, AI planner, policy version, audit chain |
| 1 | 8.0 | the goal typed in; the model proposes a four step plan with reasons |
| 2 | 18.4 | the approval card; approving; the real order is created |
| 3 | 30.1 | the PayPal sandbox checkout and the buyer approving |
| 4 | 78.9 | the capture (verified against PayPal) |
| 5 | 84.4 | deliberate interruption after the refund completes |
| 6 | 87.5 | resume: the refund is reused, the payout runs once |
| 7 | 90.3 | the hash-chained audit timeline |
| 8 | 102.1 | a payout the policy refuses (100,000 USD), zero platform writes |
| 9 | 114.0 | the money, in PayPal's own sandbox account |
| 10 | 130.1 | back to the console |

## Evidence shown in the video (read back from PayPal after the run)

| resource | id | platform status |
|---|---|---|
| order | `95H90132D52748058` | `COMPLETED` |
| capture | `6UT72402FC133551Y` | `PARTIALLY_REFUNDED` |
| refund | `7E550592AE673915F` | `COMPLETED` |
| payout | `MGPPKAXY53DVJ` | batch `SUCCESS` |
