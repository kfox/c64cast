- **Mic and webcam audio through the REU pump now plays under `mhires`
  and `hires` REU-staged video (#551).** With `[audio].use_reu_pump` on a
  mic or webcam scene whose display used REU staging, the C64 could crash
  into silence on the first scene of a run, and after an REU video scene it
  ran the earlier scene's pump instead of its own, which garbled the mic
  audio after about 20 seconds and made the run log phantom SHIFT presses.
  The mic pump now uses the same main-RAM address trackers
  as the video pump, and the REU video setup parks a safe return where the
  pump will go. Measured on an Ultimate 64 with firmware 3.15a.
  Each step of the pump install is now confirmed delivered before the next one
  starts. If a step still has not landed after three tries, the scene logs an
  error and plays without audio. Before, the pump started anyway, and on
  addresses it had never been given it could overwrite C64 memory.
