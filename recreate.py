#!/usr/bin/env python3
"""
2D Animation with Algrow: make a video in the visual style of a reference YouTube video,
narrating a script of your own.

HOW IT WORKS, IN ORDER
    1. analyze   Ask Algrow to watch the reference video and describe its style
                 (colours, medium, camera, pacing) and log every cut.
    2. plan      Ask an LLM (any LLM, see PLANNER below) to split your script into
                 beats and write one image prompt per beat in the reference style.
    3. voice     Ask Algrow's Stealth TTS to narrate the script. It returns the
                 audio plus the start time of every word, which drives the cuts.
    4. broll     Ask Algrow to generate one image per beat. Image 001 is passed as
                 a style reference to every later image so the look stays consistent.
    5. assemble  ffmpeg: give each image a slow move, cut exactly on the word
                 timings, concatenate, add the narration.

Every stage saves what it made inside the video folder and is skipped on the next
run if its output already exists. Add --force to redo a stage.

USAGE
    python3 recreate.py videos/my-video all
    python3 recreate.py videos/my-video broll --force

VIDEO FOLDER, one per video (see README for what each file looks like)
    videos/my-video/script.txt      your narration, plain text
    videos/my-video/settings.json        reference_url, voice, ...
    videos/my-video/style.json      written by analyze
    videos/my-video/shots.json      written by analyze
    videos/my-video/beats.json      written by plan (and updated by broll/assemble)
    videos/my-video/words.json      written by voice
    videos/my-video/final.mp4     the finished video
    videos/my-video/work/         audio, images, clips (not committed)

PLANNER (which LLM writes the beats), chosen by the PLANNER value in .env
    claude-cli   default if the `claude` command exists. Runs `claude -p --model opus`.
    openai       any OpenAI-compatible chat endpoint: OpenAI, Gemini, Groq, Ollama,
                 OpenRouter, Inworld... Set OPENAI_BASE_URL, OPENAI_API_KEY, PLANNER_MODEL.
    manual       writes work/plan_prompt.md, you paste it into any chat model and save
                 the JSON answer as beats.json, then run the next stage.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"


# =============================================================================
# Small helpers
# =============================================================================

def load_env():
    """Read KEY=VALUE lines from .env into the environment (no extra package needed)."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()
API_BASE = os.environ.get("ALGROW_API_BASE", "https://api.algrow.online").rstrip("/")
API_KEY = os.environ.get("ALGROW_API_KEY", "")
AUTH_HEADER = {"Authorization": f"Bearer {API_KEY}"}


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


def run_command(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=1))


def extract_json_text(text):
    """Cut a JSON object out of an LLM reply, ignoring any ```json fence around it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    first, last = text.find("{"), text.rfind("}")
    return text[first:last + 1]


def parse_json_reply(text):
    return json.loads(extract_json_text(text))


def parse_shot_log(text):
    """Parse the shot log JSON. The analysis model makes two known mistakes past the
    one minute mark: it drops the "start" key ({"n": 41, 2:41.2, "end": ...}) and it
    writes 1:00.6 instead of 60.6. Both are repaired here. If the JSON is still
    broken, parse shot by shot and keep the good ones."""
    text = extract_json_text(text)
    text = re.sub(r'("n":\s*\d+,\s*)"?(\d+(?::\d\d)?(?:\.\d+)?),(\s*"end")',
                  r'\1"start": \2,\3', text)
    text = re.sub(r'("(?:start|end)":\s*)(\d+):(\d\d(?:\.\d+)?)',
                  lambda m: f"{m.group(1)}{int(m.group(2)) * 60 + float(m.group(3)):.1f}", text)
    try:
        return json.loads(text).get("shots", [])
    except json.JSONDecodeError:
        pass
    shots, broken = [], 0
    for match in re.finditer(r"\{\s*\"n\":.*?\}(?=\s*,\s*\{\s*\"n\"|\s*\]\s*\}\s*$)", text, re.S):
        try:
            shots.append(json.loads(match.group(0)))
        except json.JSONDecodeError:
            broken += 1
    log(f"  shot log: kept {len(shots)} shots, dropped {broken} unreadable ones")
    return shots


# =============================================================================
# Algrow API: three endpoints and one job poller
# =============================================================================

def algrow_post(path, json_body=None, form=None):
    """POST to the Algrow API. Every generation endpoint answers with a job_id."""
    response = requests.post(API_BASE + path, headers=AUTH_HEADER, json=json_body, data=form, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"{path} -> HTTP {response.status_code}: {response.text[:300]}")
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"{path} -> {data}")
    return data


def wait_for_job(job_id, label, poll_seconds=5, max_seconds=2400):
    """Poll /api/job-status until the job is completed. Returns the final status payload."""
    started = time.time()
    while time.time() - started < max_seconds:
        response = requests.get(f"{API_BASE}/api/job-status/{job_id}", headers=AUTH_HEADER, timeout=60)
        data = response.json() if response.status_code == 200 else {}
        if data.get("status") == "completed":
            return data
        if data.get("status") == "failed":
            raise RuntimeError(f"{label} failed: {data.get('error_message') or data.get('error')}")
        time.sleep(poll_seconds)
    raise RuntimeError(f"{label} timed out after {max_seconds}s")


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_content(1 << 16):
                f.write(chunk)
    return dest


def analyze_video(video_url, prompt, resolution):
    """One call to Algrow video analysis. Billed per 4 minutes of the video's full length,
    times 3 at 'default' resolution, so 'low' is the sensible choice here."""
    submitted = algrow_post("/api/analyze-video", {
        "video_url": video_url, "prompt": prompt, "media_resolution": resolution,
    })
    log(f"  analysis queued, {submitted.get('credits_charged', 0)} credits")
    return submitted, wait_for_job(submitted["job_id"], "video analysis")


def generate_image(prompt, model, aspect_ratio, reference_urls, dest):
    """One Algrow image. reference_urls keeps later images in the style of the first."""
    body = {"prompt": prompt, "model": model, "aspect_ratio": aspect_ratio}
    if reference_urls:
        body["reference_image_urls"] = reference_urls
    submitted = algrow_post("/api/generate-image", body)
    result = wait_for_job(submitted["job_id"], f"image {dest.stem}")
    url = (result.get("image_urls") or [None])[0]
    if not url:
        raise RuntimeError(f"image {dest.stem}: no image in {result}")
    download(url, dest)
    return url


def generate_voice(script, voice, title):
    """Stealth TTS. Returns (audio_url, transcript_url). The transcript is an SRT with
    one word per cue, which is where every cut in the video comes from."""
    submitted = algrow_post("/api/generate-simple", form={
        "script": script, "provider": "stealth", "voice_id": voice, "custom_title": title,
    })
    result = wait_for_job(submitted["job_id"], "voice")
    if not result.get("transcript_url"):
        raise RuntimeError("voice job returned no word-level transcript")
    return result["audio_url"], result["transcript_url"]


# =============================================================================
# The planner: any LLM. Takes a prompt string, returns the reply text.
# =============================================================================

def ask_llm(prompt):
    backend = os.environ.get("PLANNER") or ("claude-cli" if shutil.which("claude") else "manual")
    if backend == "claude-cli":
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(["claude", "-p", "--model", os.environ.get("PLANNER_MODEL", "opus"),
                                 "--output-format", "text"],
                                input=prompt, capture_output=True, text=True, env=env, timeout=900)
        if result.returncode:
            raise RuntimeError(f"claude CLI failed: {result.stderr[-400:]}")
        return result.stdout
    if backend == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.environ.get("PLANNER_MODEL")
        if not model:
            raise RuntimeError("PLANNER=openai needs PLANNER_MODEL in .env")
        response = requests.post(f"{base}/chat/completions",
                                 headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
                                 json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                                 timeout=900)
        if response.status_code != 200:
            raise RuntimeError(f"{base} -> HTTP {response.status_code}: {response.text[:300]}")
        return response.json()["choices"][0]["message"]["content"]
    if backend == "manual":
        raise ManualPlanner(prompt)
    raise RuntimeError(f"unknown PLANNER '{backend}' (use claude-cli, openai, or manual)")


class ManualPlanner(Exception):
    """Raised when PLANNER=manual: the prompt is written to disk for the user to run."""


# =============================================================================
# Stage 1: analyze the reference video
# =============================================================================

def stage_analyze(job, config, force):
    style_file, shots_file = job / "style.json", job / "shots.json"
    url = config["reference_url"]
    resolution = config.get("analysis_resolution", "low")

    if force or not style_file.exists():
        log("style pass: medium, palette, camera, pacing")
        submitted, result = analyze_video(url, (PROMPTS / "style.txt").read_text(), resolution)
        style = parse_json_reply(result["analysis_text"])
        style["meta"]["duration_seconds"] = submitted["duration_seconds"]
        write_json(style_file, style)
    style = read_json(style_file)

    if force or not shots_file.exists():
        # One call per 4 minute window so a long video does not overflow one reply.
        duration = style["meta"]["duration_seconds"]
        window = 240
        windows = [(start, min(start + window, duration)) for start in range(0, int(duration), window)]
        if len(windows) > 1 and windows[-1][1] - windows[-1][0] < 60:
            windows[-2] = (windows[-2][0], windows[-1][1])   # a tiny tail window costs a full call
            windows.pop()
        log(f"shot log: {len(windows)} windows of {window}s")
        template = (PROMPTS / "shots.txt").read_text()
        pending = []
        for start, end in windows:
            prompt = template.replace("{START}", str(start)).replace("{END}", str(int(end)))
            submitted = algrow_post("/api/analyze-video", {"video_url": url, "prompt": prompt,
                                                           "media_resolution": resolution})
            log(f"  window {start}-{int(end)} queued, {submitted.get('credits_charged', 0)} credits")
            pending.append((start, end, submitted["job_id"]))
        shots = []
        for start, end, job_id in pending:
            result = wait_for_job(job_id, f"shots {start}-{int(end)}")
            shots.extend(parse_shot_log(result["analysis_text"]))
        shots.sort(key=lambda s: float(s.get("start", 0)))
        for number, shot in enumerate(shots, 1):
            shot["n"] = number
        write_json(shots_file, {"shots": shots})

    shots = read_json(shots_file)["shots"]
    log(f"analyze done: {style['visual_medium']['type']}, {len(shots)} cuts, "
        f"average shot {style['meta']['average_shot_seconds']}s")


# =============================================================================
# Stage 2: plan the beats with an LLM
# =============================================================================

def squash_spaces(text):
    return re.sub(r"\s+", " ", text).strip()


def build_plan_prompt(job):
    script = (job / "script.txt").read_text().strip()
    style = read_json(job / "style.json")
    shots = read_json(job / "shots.json")["shots"]
    meta = style["meta"]
    # A spread of 40 sample shots is plenty for the LLM to learn the reference's habits.
    sample = shots[:: max(1, len(shots) // 40)][:40]
    sample = [{k: s.get(k) for k in ("start", "end", "framing", "angle", "subject", "motion", "image_prompt")}
              for s in sample]
    style_view = {k: style[k] for k in ("visual_medium", "palette", "lighting", "camera",
                                        "motion", "on_screen_text", "subjects")}
    average_shot = float(meta.get("average_shot_seconds") or 5)
    return ((PROMPTS / "plan.md").read_text()
            .replace("{STYLE}", json.dumps(style_view, indent=1))
            .replace("{SHOTS}", json.dumps(sample, indent=1))
            .replace("{SCRIPT}", script)
            .replace("{AVG_SHOT}", str(average_shot))
            .replace("{WORDS_PER_BEAT}", str(int(round(average_shot * 2.5))))   # ~2.5 words/second
            .replace("{MIN_SHOT}", str(meta.get("shortest_shot_seconds") or 2))
            .replace("{MAX_SHOT}", str(meta.get("longest_shot_seconds") or 12)))


def check_beats(beats, script):
    """The beat texts joined together must be the script, word for word. Otherwise the
    word timings from the voice stage would not line up with the beats."""
    return squash_spaces(" ".join(b["text"] for b in beats)) == squash_spaces(script)


def stage_plan(job, config, force):
    beats_file = job / "beats.json"
    if beats_file.exists() and not force:
        log("plan: beats.json already exists, skipping")
        return
    script = (job / "script.txt").read_text().strip()
    prompt = build_plan_prompt(job)
    prompt_file = job / "work" / "plan_prompt.md"
    prompt_file.write_text(prompt)

    for attempt in (1, 2):
        log(f"plan: asking the LLM (attempt {attempt})")
        try:
            reply = ask_llm(prompt)
        except ManualPlanner:
            print(f"\nPLANNER=manual. Paste the contents of {prompt_file} into any chat model,\n"
                  f"save its JSON answer as {beats_file}, then run the next stage.\n")
            sys.exit(0)
        beats = parse_json_reply(reply)["beats"]
        if check_beats(beats, script):
            break
        log("plan: the LLM changed the script text, asking again")
        prompt += ("\n\nYour previous answer changed the script text. The concatenated beat texts "
                   "MUST equal the script exactly.")
    else:
        raise RuntimeError("the LLM could not split the script without altering it")

    for number, beat in enumerate(beats, 1):
        beat["n"] = number
    write_json(beats_file, {"beats": beats})
    log(f"plan done: {len(beats)} beats")


# =============================================================================
# Stage 3: voice
# =============================================================================

def parse_word_srt(text):
    """Stealth's transcript is an SRT with one word per cue -> [{text, start, end}]."""
    words = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        words.append({"text": " ".join(lines[2:]).strip(),
                      "start": h1 * 3600 + m1 * 60 + s1 + ms1 / 1000,
                      "end": h2 * 3600 + m2 * 60 + s2 + ms2 / 1000})
    return words


def snap_words_to_script(cues, script):
    """The voice engine sometimes splits one written word into two cues ("2000" ->
    "20" + "00"). Walk the script's own words and merge cues until they spell each one,
    so captions show the script's spelling and beat matching sees the same tokens."""
    letters = lambda t: re.sub(r"[^A-Za-z0-9]", "", t).lower()
    words, i = [], 0
    for token in script.split():
        target = letters(token)
        if not target or i >= len(cues):
            continue
        start, end, spelled = cues[i]["start"], cues[i]["end"], letters(cues[i]["text"])
        i += 1
        while spelled != target and i < len(cues) and target.startswith(spelled + letters(cues[i]["text"])):
            spelled += letters(cues[i]["text"])
            end = cues[i]["end"]
            i += 1
        if spelled != target:                     # engine said something else; keep the cue as is
            words.append({"text": cues[i - 1]["text"], "start": start, "end": end})
            continue
        words.append({"text": token, "start": start, "end": end})
    return words


def stage_voice(job, config, force):
    audio_file, words_file = job / "work" / "narration.mp3", job / "words.json"
    if audio_file.exists() and words_file.exists() and not force:
        log("voice: narration already exists, skipping")
        return
    script = (job / "script.txt").read_text().strip()
    voice = config.get("voice", "Edward")
    log(f"voice: Stealth, voice {voice}, {len(script)} characters")
    audio_url, transcript_url = generate_voice(script, voice, f"recreate-{job.name}")
    download(audio_url, audio_file)
    srt = requests.get(transcript_url, timeout=60).text
    (job / "work" / "narration.srt").write_text(srt)
    words = snap_words_to_script(parse_word_srt(srt), script)
    write_json(words_file, words)
    log(f"voice done: {len(words)} words, {media_duration(audio_file):.2f}s")


# =============================================================================
# Stage 4: b-roll images
# =============================================================================

def stage_broll(job, config, force):
    beats_file = job / "beats.json"
    beats = read_json(beats_file)["beats"]
    style = read_json(job / "style.json")
    stills_dir = job / "work" / "stills"
    stills_dir.mkdir(parents=True, exist_ok=True)
    model = "gpt-image-2"          # cheapest Algrow image model and the one this pipeline is tuned for
    aspect = config.get("aspect_ratio", "16:9")
    prefix = style.get("prompt_prefix") or ""        # one sentence describing the reference style
    negative = style.get("negative_prompt") or ""

    def full_prompt(beat):
        text = f"{prefix} {beat['image_prompt']}"
        return f"{text} Avoid: {negative}." if negative else text

    def make_still(beat, reference_urls):
        dest = stills_dir / f"{beat['n']:03d}.png"
        if dest.exists() and beat.get("image_url") and not force:
            return beat["image_url"]
        beat["image_url"] = generate_image(full_prompt(beat), model, aspect, reference_urls, dest)
        log(f"  image {beat['n']:03d} done")
        return beat["image_url"]

    log(f"broll: {len(beats)} images on {model}")
    anchor_url = make_still(beats[0], None)          # image 001 sets the look
    write_json(beats_file, {"beats": beats})
    with ThreadPoolExecutor(max_workers=int(config.get("image_workers", 4))) as pool:
        list(pool.map(lambda beat: make_still(beat, [anchor_url]), beats[1:]))
    write_json(beats_file, {"beats": beats})
    log("broll done")


# =============================================================================
# Stage 5: assemble with ffmpeg
# =============================================================================

def media_duration(path):
    out = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "csv=p=0", str(path)]).stdout
    return float(out.strip() or 0)


def count_frames(path):
    out = run_command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                       "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)]).stdout
    return int(out.strip() or 0)


def beat_start_times(beats, words):
    """Each beat starts when its first word is spoken. Walk the word list in order so a
    repeated word ("the") is matched at the right place. Beat 1 always starts at 0."""
    tokens = lambda s: re.findall(r"[A-Za-z0-9']+", s.lower())
    starts, cursor = [], 0
    for beat in beats:
        beat_tokens = tokens(beat["text"])
        if not beat_tokens:
            starts.append(starts[-1] if starts else 0.0)
            continue
        i = cursor
        while i < len(words) and tokens(words[i]["text"])[:1] != beat_tokens[:1]:
            i += 1
        if i >= len(words):
            raise RuntimeError(f"beat {beat['n']}: word '{beat_tokens[0]}' not found in the narration")
        starts.append(words[i]["start"])
        cursor = i + len(beat_tokens) - 1
    starts[0] = 0.0
    return starts


def still_to_clip(still, dest, frames, width, height, fps, motion, zoom_pct):
    """Turn one image into a clip of exactly `frames` frames with a slow camera move.
    The image is oversampled to 2x so the move stays smooth."""
    zoom = 1 + zoom_pct / 100.0
    n = max(frames, 1)
    big_w, big_h = width * 2, height * 2
    centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    moves = {
        "zoom_in":   (f"1+({zoom}-1)*on/{n}", centre_x, centre_y),
        "zoom_out":  (f"{zoom}-({zoom}-1)*on/{n}", centre_x, centre_y),
        "pan_left":  (f"{zoom}", f"(iw-iw/zoom)*(1-on/{n})", centre_y),
        "pan_right": (f"{zoom}", f"(iw-iw/zoom)*on/{n}", centre_y),
    }
    z, x, y = moves.get(motion, ("1", "0", "0"))   # anything else = static
    filters = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
               f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={width}x{height}:fps={fps},format=yuv420p")
    result = run_command(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(still),
                          "-vf", filters, "-frames:v", str(n), "-r", str(fps),
                          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(dest)])
    if result.returncode or not dest.exists():
        raise RuntimeError(f"clip {dest.name}: {result.stderr[-300:]}")
    got = count_frames(dest)
    if got != n:
        raise RuntimeError(f"clip {dest.name}: wanted {n} frames, got {got}")


def stage_assemble(job, config, force):
    work = job / "work"
    final = job / "final.mp4"
    if final.exists() and not force:
        log("assemble: final.mp4 already exists, skipping")
        return
    width, height, fps = int(config.get("width", 1920)), int(config.get("height", 1080)), int(config.get("fps", 30))
    zoom_pct = float(config.get("zoom_pct", 8))
    beats = read_json(job / "beats.json")["beats"]
    words = read_json(job / "words.json")
    style = read_json(job / "style.json")
    narration = work / "narration.mp3"
    total_seconds = media_duration(narration)
    clips_dir = work / "clips"
    clips_dir.mkdir(exist_ok=True)

    # Cuts are anchored to frame numbers. Each clip runs from its beat's start frame to
    # the next beat's start frame, so rounding can never drift over a long video.
    starts = beat_start_times(beats, words)
    total_frames = int(round(total_seconds * fps))
    anchors = [int(round(s * fps)) for s in starts] + [total_frames]
    clips = []
    for beat, start, frame_from, frame_to in zip(beats, starts, anchors, anchors[1:]):
        frames = frame_to - frame_from
        beat["start"], beat["frames"] = round(start, 3), frames
        if frames <= 0:
            log(f"  beat {beat['n']} has no frames, skipped")
            continue
        dest = clips_dir / f"{beat['n']:03d}.mp4"
        if not dest.exists() or force:
            still_to_clip(work / "stills" / f"{beat['n']:03d}.png", dest, frames,
                          width, height, fps, beat.get("motion", "zoom_in"), zoom_pct)
        log(f"  beat {beat['n']:03d} starts {start:6.2f}s, {frames} frames, {beat.get('motion')}")
        clips.append(dest)
    write_json(job / "beats.json", {"beats": beats})

    concat_list = work / "concat.txt"
    concat_list.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    silent_video = work / "video.mp4"
    result = run_command(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                          "-safe", "0", "-i", str(concat_list), "-c", "copy", str(silent_video)])
    if result.returncode:
        raise RuntimeError(f"concat: {result.stderr[-300:]}")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent_video), "-i", str(narration)]
    captions = style.get("on_screen_text") or {}
    if captions.get("captions_present"):
        ass = write_captions(words, captions.get("caption_style") or {}, width, height, work / "captions.ass")
        cmd += ["-vf", f"ass={ass}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-map", "0:v:0", "-map", "1:a:0",
            "-movflags", "+faststart", str(final)]
    result = run_command(cmd)
    if result.returncode:
        raise RuntimeError(f"mux: {result.stderr[-300:]}")

    video_seconds = media_duration(final)
    log(f"assemble done: {video_seconds:.3f}s of video for {total_seconds:.3f}s of narration "
        f"({count_frames(final)} frames) -> {final}")
    if abs(video_seconds - total_seconds) > 1.0 / fps + 0.05:
        raise RuntimeError("final video length does not match the narration")


def write_captions(words, caption_style, width, height, out):
    """Burned-in word-group captions, only used when the reference video has them."""
    group = int(caption_style.get("words_per_line") or 4)
    align = {"bottom": 2, "center": 5, "top": 8}.get(caption_style.get("position") or "bottom", 2)
    # The analysis describes the font in words ("Monospace Courier / Terminal"); map
    # that to a font that is actually installed, or libass silently falls back.
    described = (caption_style.get("font") or "").lower()
    font = "Liberation Mono" if re.search(r"mono|courier|terminal|typewriter", described) else "DejaVu Sans"
    size = int(height * 0.075)
    colour = (caption_style.get("colour_hex") or "#FFFFFF").lstrip("#")
    bgr = colour[4:6] + colour[2:4] + colour[0:2] if len(colour) == 6 else "FFFFFF"

    def stamp(seconds):
        cs = int(round(max(seconds, 0) * 100))
        h, cs = divmod(cs, 360000)
        m, cs = divmod(cs, 6000)
        s, cs = divmod(cs, 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    lines = [f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00{bgr},&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{round(size / 14, 1)},{max(1, size // 36)},{align},80,80,{int(height * 0.1)},1

[Events]
Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text
"""]
    groups = [words[i:i + group] for i in range(0, len(words), group)]
    for index, g in enumerate(groups):
        start = g[0]["start"]
        end = groups[index + 1][0]["start"] if index + 1 < len(groups) else g[-1]["end"] + 0.6
        text = " ".join(w["text"] for w in g)
        if caption_style.get("uppercase"):
            text = text.upper()
        text = text.replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{stamp(start)},{stamp(max(end, start + 0.3))},Cap,0,0,0,,{{\\fad(60,40)}}{text}")
    out.write_text("\n".join(lines) + "\n")
    return out


# =============================================================================
# Command line
# =============================================================================

STAGES = [("analyze", stage_analyze), ("plan", stage_plan), ("voice", stage_voice),
          ("broll", stage_broll), ("assemble", stage_assemble)]


def main():
    if len(sys.argv) < 3 or sys.argv[2] not in ["all"] + [name for name, _ in STAGES]:
        sys.exit(__doc__)
    job = Path(sys.argv[1]).resolve()
    wanted = sys.argv[2]
    force = "--force" in sys.argv
    if not API_KEY:
        sys.exit("ALGROW_API_KEY is not set. Copy .env.example to .env and fill it in.")
    config = read_json(job / "settings.json")
    (job / "work").mkdir(exist_ok=True)
    for name, stage in STAGES:
        if wanted in ("all", name):
            log(f"== {name}")
            stage(job, config, force)


if __name__ == "__main__":
    main()
