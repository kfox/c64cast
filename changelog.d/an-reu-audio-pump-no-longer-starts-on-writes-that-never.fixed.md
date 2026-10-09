- **An REU audio pump no longer starts on writes that never reached the C64.**
  On petscii and blank REU video scenes, and for the tail of every REU pump
  setup, a write lost on the network (a dropped or redialed DMA connection)
  went unnoticed, and the C64's interrupt was pointed at the pump anyway. It
  could then run leftover code or copy audio into color RAM. Each of those
  writes is now confirmed and retried; if one still does not land, the scene
  logs an error and plays without audio.
