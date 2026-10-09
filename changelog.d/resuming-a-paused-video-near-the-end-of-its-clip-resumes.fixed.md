- **Resuming a paused video near the end of its clip resumes it instead of
  ending the scene.** The video decoder reads several seconds ahead and stopped
  for good when it reached the end of the file, so a seek made after that was
  never carried out. The same fix covers an A/B loop that wraps in those last
  seconds, which used to hold a frozen frame, and a seek back from near the
  end.
