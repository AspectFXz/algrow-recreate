# 2D Animation with Algrow

![Example: the first 14 seconds of a video made by this pipeline](docs/example.gif)

*The example above was made from the script in `videos/example-vikings-heat/`, copying the style of [this video](https://www.youtube.com/watch?v=FkK6xKrY-qM). [Watch the full 38 seconds with sound.](https://audio.algrow.online/studio/videos/recreate/vikings-heat/vikings-heat_20260903_232807.mp4)*

**Make a 15 minute video in the style of any YouTube video for about $2.40.**
That is roughly 76 Algrow credits: about 20 to analyze the reference video and about 56 for the images.
The voice is included in your plan. You need a Professional ($45 a month) or Ultimate ($80 a month) plan.

| Plan | Price | 15 minute videos included every month | Each extra video |
|---|---|---|---|
| Professional | $45 | 3 | about $2.40 in credits |
| Ultimate | $80 | 7 | about $2.40 in credits |

Full cost breakdown further down. Public domain: take it, change it, ship it.

```
reference video ──analyze──▶ style.json + shots.json      (Algrow video analysis)
                                    │
script.txt ────────plan─────▶ beats.json                  (any LLM: splits the script into shots)
     │                              │
     └────────voice────────▶ narration.mp3 + words.json  (Algrow Stealth TTS, word timings included)
                                    │
                            broll ──▶ work/stills/NNN.png (Algrow image generation, style-locked to image 001)
                                    │
                          assemble ─▶ final.mp4           (ffmpeg on your machine)
```

## Run it in five minutes

You need Python 3, ffmpeg, an Algrow API key, and one LLM for the planning step (any LLM, see below).

```
git clone https://github.com/samalgrow/algrow-2d-animation
cd algrow-2d-animation
pip install requests boto3
cp .env.example .env        # paste your Algrow API key, choose a planner
```

Make a folder for your video with two files in it:

```
mkdir videos/my-video
cp videos/example-vikings-heat/settings.json videos/my-video/     # then edit reference_url
nano videos/my-video/script.txt                     # your narration, plain text
python3 recreate.py videos/my-video all
```

The finished video is `videos/my-video/final.mp4`.

Every stage saves its result inside the video folder, so running the command again only
redoes what is missing. To redo one stage: `python3 recreate.py videos/my-video broll --force`.

## Any LLM works for planning

The plan stage is the only step that is not an Algrow call. It sends one prompt
(`prompts/plan.md`, filled in with the analysis and your script) to an LLM and expects
JSON back. Set `PLANNER` in `.env`:

| PLANNER | What happens | Needs |
|---|---|---|
| `claude-cli` | runs the `claude` command from Claude Code | Claude Code installed and logged in |
| `openai` | POSTs to any OpenAI-compatible chat endpoint | `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `PLANNER_MODEL` |
| `manual` | writes `work/plan_prompt.md` and stops | you paste it into any chat, save the reply as `beats.json` |

`openai` has been tested through the Inworld router with an OpenAI model. Gemini, Groq, Ollama,
OpenRouter and most others expose the same endpoint, so just change the base URL and model.

## What each stage does

**analyze** sends the reference video to Algrow twice over. The first prompt
(`prompts/style.txt`) returns a style record: medium, colours, lighting, camera mix,
motion, captions, and a one-sentence `prompt_prefix` that is put in front of every image
prompt later. The second prompt (`prompts/shots.txt`) logs every cut with a timestamp,
framing, subject and an image prompt, one call per 4 minutes of video.

**plan** gives the LLM the style record, a sample of the shot log, and your script, and
asks for beats: which sentence goes with which image, and the image prompt for each.
The beat texts must add up to the script word for word, and the code checks that, because
the word timings from the next stage are matched against the beat text.

**voice** narrates the script with Algrow's Stealth voice. Stealth returns a transcript
with a start and end time for every word. That is what decides where every cut lands.

**broll** generates one image per beat. Images are always made with gpt-image-2. Image 001 is generated first and then passed to
every other image as a style reference, which is what keeps 160 images looking like one
video.

**assemble** turns each image into a clip of exactly the right number of frames with a slow
zoom or pan, joins them, and adds the narration. Cuts are anchored to frame numbers so a
long video never drifts. If the reference burned captions in, captions are burned in here
in the same style. The stage refuses to finish if the video and the narration differ in
length by more than one frame.

## The video folder

```
videos/my-video/
  script.txt        your narration, plain text (you write this)
  settings.json          settings (you write this, copy the example)
  style.json        what the reference looks like            written by analyze
  shots.json        every cut in the reference               written by analyze
  beats.json        your script split into shots + prompts   written by plan
  words.json        start and end time of every spoken word  written by voice
  final.mp4         the finished video                       written by assemble
  work/             audio, images, clips                     not committed to git
```

`settings.json` keys:

| Key | Default | Meaning |
|---|---|---|
| `reference_url` | | the YouTube video to copy the style of |
| `voice` | `Edward` | Stealth voice name |
| `aspect_ratio` | `16:9` | |
| `width`, `height`, `fps` | 1920, 1080, 30 | output video |
| `zoom_pct` | 8 | how far each image zooms or pans over its clip |
| `image_workers` | 4 | how many images to generate at once |
| `analysis_resolution` | `low` | `default` sees more detail but costs 3x the analysis credits |

## If something goes wrong

- **"ALGROW_API_KEY is not set"**: copy `.env.example` to `.env` and paste your key.
- **Voice stage returns 403**: voice through the API needs a Professional or Ultimate plan.
- **"the LLM could not split the script without altering it"**: the planner rewrote your
  words. Try a stronger model, or use `PLANNER=manual` and fix the JSON by hand.
- **"word ... not found in the narration"**: the script has a token the voice did not say the
  same way (numbers, symbols). Write numbers out as words in `script.txt`.
- **A still looks wrong**: delete `work/stills/NNN.png`, remove its `image_url` in `beats.json`,
  and run `broll` again. Only that image is regenerated.
- **Reference video is very long**: analysis is billed on its full length. Pick a shorter
  reference, or trim the number of shot-log windows in `stage_analyze`.


## What it costs

Algrow bills three things in this pipeline. For a 15 minute video made from a 15 minute reference:

| What | How it is billed | Per 15 minute video |
|---|---|---|
| Analyzing the reference video | 1 credit per 4 minutes of the reference, per call. The pipeline makes 5 calls. | about 20 credits |
| The voice | Stealth characters from your plan's monthly pool (2 million on Professional, 4 million on Ultimate) | about 13,000 characters, so included |
| The images | gpt-image-2 at 0.35 credits per image, about 160 images | about 56 credits |

Total: about 76 credits. Credits are about 3 cents each, so about $2.40 per video.

Two things that change the number:

- A longer reference video costs more to analyze. Analysis is priced on the reference's length, not your script's.
- Setting `analysis_resolution` to `default` in `settings.json` triples the analysis credits. The pipeline uses `low`, which found the same number of cuts in testing.

### What you need

A Professional plan ($45 a month) or an Ultimate plan ($80 a month). The Starter plan cannot generate voice through the API, so it cannot run this pipeline.

### Videos included in your plan, no extra payment

| Plan | Price | Credits included | 15 minute videos per month |
|---|---|---|---|
| Professional | $45 | 300 | 3 |
| Ultimate | $80 | 535 | 7 |

### Want more? Buy credits

Credit top-ups: 150 credits for $4.99, 405 for $12.99, or 1,000 for $31.49.
The 1,000 pack is the best value: it buys 13 more videos.

What you would pay in total each month, plan included:

| Videos per month | On Professional | On Ultimate |
|---|---|---|
| 5 | $49.99 | $80.00 |
| 10 | $76.49 | $92.99 |
| 20 | $89.48 | $111.49 |
| 50 | $170.96 | $187.46 |
| 100 | $278.42 | $305.42 |

The voice pool is not a hard limit either. Professional includes enough Stealth characters for
153 videos a month and Ultimate for 307. Past that, Stealth top-ups are 500,000 characters for $5,
2 million for $20, or 5 million for $50. At 13,000 characters per video that is about 13 cents of
voice per video, so a $20 pack covers 153 more videos.

Real numbers from the example video in this repo: a 38 second video, 11 images, cost 3.85 credits for the images. Analyzing its 12 minute reference costs 3 credits per call, 12 credits for the 4 calls.


## License

Public domain under the [Unlicense](LICENSE). No attribution needed.
