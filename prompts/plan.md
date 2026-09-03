You are planning the b-roll for a narrated video. The video must look like a REFERENCE video, shot for shot in style and pacing, but it narrates a NEW script.

## Reference style (from Algrow video analysis)
{STYLE}

## Sample of the reference shot log (framing, subject, and image prompts as they appear in the reference)
{SHOTS}

## The new script (narrate verbatim, do not edit it)
{SCRIPT}

## Your job
Split the script into consecutive beats and write one image prompt per beat.

Rules:
1. Beat text: the beats, concatenated in order, must reproduce the script EXACTLY, character for character, including punctuation. Cut only at sentence or clause boundaries. Never rewrite, drop, or add words.
2. Pacing: the reference averages {AVG_SHOT} seconds per shot. Narration runs about 2.5 words per second, so aim for about {WORDS_PER_BEAT} words per beat. Vary beat length the way the reference does, shortest about {MIN_SHOT}s, longest about {MAX_SHOT}s.
3. Framing mix: match the reference percentages for wide, medium and close-up shots and for camera angles.
4. Subjects: follow the reference's conventions for people, settings and objects. Do not introduce subjects the reference style would never show.
5. Image prompts: 40 to 80 words each. Concrete nouns, one clear focal point, describe composition, subject, setting, lighting and colour. Do NOT describe style or medium; a style prefix is added automatically. No words or letters inside the image unless the reference uses labels, and then say exactly which label text.
6. Beat 1 is the anchor: every later image is generated with beat 1's image as a style reference, so make beat 1 a representative, medium-wide establishing shot.
7. motion: choose from static, zoom_in, zoom_out, pan_left, pan_right, following the reference's habits.

Return ONLY one valid JSON object, no markdown fence, no commentary:
{"beats": [{"n": 1, "text": "exact script slice", "framing": "wide|medium|close_up|macro", "angle": "eye_level|low|high|top_down", "motion": "static|zoom_in|zoom_out|pan_left|pan_right", "image_prompt": "..."}]}
