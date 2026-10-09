- **The REU mic path starts with its full intended lead.** The host's write
  head was placed relative to the ring start, so by the time the stream
  opened the pump had already consumed part of that lead and the microphone
  ran closer to underrun than the bootstrap margin promised. The head is now
  placed relative to where the pump actually is. Measured on an Ultimate 64
  in mhires, the lead at stream open is exactly 1600 B. The lead servo's
  first reading comes one interval later and shows that opening lead plus
  about a second of the pump's roughly 15 % shortfall (3.3-3.8 KB in the
  measured runs); the startup peak (about 6 KB in mhires) is the servo
  design's existing limit and is unchanged.
