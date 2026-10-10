- **On REU-staged `hires` video, the border color now changes with the
  picture at a cut instead of several fields ahead of it.** The C64 now
  writes the border as it switches to the frame, instead of the host
  writing it when the frame is sent. The red border shown while a loop is
  armed still holds until the video's border color changes. On host-DMA
  double-buffered `hires`, the border is written just before the frame is
  armed, so it leads the picture by less than a field.
