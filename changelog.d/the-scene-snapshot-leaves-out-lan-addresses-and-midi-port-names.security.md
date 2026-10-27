- **The scene snapshot no longer publishes a WLED sink's trusted addresses or a
  MIDI port's name.** `SCENE_CONFIG_JSON`, written to be pasted into a public
  video description, listed a `wled` scene's `sink_allow` addresses (the only
  barrier against frame injection, and so exactly the source address a LAN
  peer would need to forge) and a `midi` or `asid` scene's port name, which
  often carries a device's or its owner's name. All three are now left out,
  as the connection target already was.
