# videos

One folder per video you make. To start a new one:

```
mkdir videos/my-video
cp videos/example-vikings-heat/settings.json videos/my-video/   # edit reference_url
nano videos/my-video/script.txt                                 # your narration
python3 recreate.py videos/my-video all
```

`example-vikings-heat/` (2D cartoon, no captions) and `example-gravano/` (3D mannequin true-crime style with burned-in captions) are finished runs you can read through: the reference video's
style record, its shot log, the script split into beats, and the word timings. The
generated audio, images and video are not committed.
