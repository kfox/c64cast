- **Video, pictures and the webcam are no longer drawn squeezed.** Each source
  was cropped to 320x200 as if a C64 pixel were square, so a 4:3 picture came
  out narrow, most of all on NTSC. The crop now follows the shape the screen is
  actually seen at: 1.2:1 for a real NTSC VIC-II on a 4:3 set, 4:3 for an NTSC
  Ultimate 64 over HDMI, and 1.5:1 for PAL. The new `[hardware].pixel_aspect`
  setting picks the output by hand (`composite`, `hdmi`, or `square` for the
  old crop) when the default guesses wrong, such as an Ultimate 64 watched over
  its analog output.
