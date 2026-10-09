- **After a seek, one badly stamped audio packet no longer slows the
  recovery from a gap in the sound for the rest of the file.** A packet
  stamped far behind its picture widened the margin the silence fill keeps
  for as long as the file played; a seek now starts that measurement over.
