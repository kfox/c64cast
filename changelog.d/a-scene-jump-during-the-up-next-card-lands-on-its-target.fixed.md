- **A scene jump pressed while the "UP NEXT" card was up played the wrong
  scene.** A MIDI jump pad, a web-console jump or a WLED effect change landing
  during the card only ended the card early, so the scene it announced played
  instead, and the jump then fired on its own whenever that scene finished,
  minutes later. It now goes to the requested scene as soon as the card ends.
