- **Ending a video scene while its stream has stalled no longer crashes
  c64cast.** When a network stream stopped sending mid-playback, the decoder
  thread waited inside FFmpeg for up to 30 seconds. A skip, a stop or the
  scene's own end in that window closed the stream under it, and the whole
  process died of a segmentation fault when the wait ended, mid-DMA. The
  stream is now closed by whichever of the two lets go of it last.
