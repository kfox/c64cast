# assets/audio/

Clean, known-good audio test clips for the **audio-quality initiative** — the
host DSP A/B work (compressor/limiter, expander, AGC, pre-emphasis) on the
4-bit `$D418` DAC path. Used by [`scripts/diags/dsp_ab.py`](../../scripts/diags/dsp_ab.py)
for offline analysis and by hand-written video-scene configs for the
real-hardware A/B.

These are deliberately **clean source recordings** (unlike the vintage
videos in [`../videos/`](../videos/), whose own noisy/band-limited audio
would confound a measurement of what the DSP itself does). The clips here are
the controlled material: any artifact you hear is the DSP + the 6581 DAC, not
the source.

As with the rest of `assets/`, the files themselves are `.gitignore`d — only
this README is tracked. Re-fetch with the commands below.

## Clips + provenance

| File | What | Source | License |
|------|------|--------|---------|
| `OSR_us_male_0030_8k.wav`   | Clean male speech, 8 kHz/16-bit mono   | [Open Speech Repository](http://www.voiptroubleshooter.com/open_speech/) | Free to use/publish; **must credit "Open Speech Repository"** |
| `OSR_us_female_0010_8k.wav` | Clean female speech, 8 kHz/16-bit mono | [Open Speech Repository](http://www.voiptroubleshooter.com/open_speech/) | same |
| `KevinMacLeod_Carefree.mp3` | Music, 44.1 kHz stereo 256 kbps        | [Kevin MacLeod / incompetech](https://incompetech.com/) | **CC-BY 4.0** — attribution required |
| `KevinMacLeod_Wallpaper.mp3`| Music, 44.1 kHz stereo 320 kbps        | [Kevin MacLeod / incompetech](https://incompetech.com/) | **CC-BY 4.0** — attribution required |

8 kHz speech is intentional: it is exactly the DAC's sample rate, so it feeds
the pipeline with no resampling artifacts (telephony band, but ideal for the
compressor/expander/AGC). The 44.1 kHz music exercises the full band (and the
pre-emphasis shelf) before the pipeline downsamples to 8 kHz.

## Re-fetch

```bash
mkdir -p assets/audio
# Open Speech Repository — clean speech (8 kHz, public/permissive)
curl -o assets/audio/OSR_us_male_0030_8k.wav \
  http://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_0030_8k.wav
curl -o assets/audio/OSR_us_female_0010_8k.wav \
  http://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_0010_8k.wav
# Kevin MacLeod — CC-BY music (credit "Kevin MacLeod (incompetech.com)")
curl -o assets/audio/KevinMacLeod_Carefree.mp3 \
  https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3
curl -o assets/audio/KevinMacLeod_Wallpaper.mp3 \
  https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wallpaper.mp3
```

## Other good sources

- **Kaggle speech-noise-dataset** — clean speech *plus* matched noise files,
  ideal for stress-testing the expander/AGC noise behavior:
  <https://www.kaggle.com/datasets/abdullahhaydarkadolu/speech-noise-dataset>
  (needs a Kaggle login; drop the uncompressed `.wav`s here).
- [voxserv/audio_quality_testing_samples](https://github.com/voxserv/audio_quality_testing_samples)
  — speech at 8/16/44.1/48 kHz with leading/trailing silence (handy for the
  expander's gate behavior).
