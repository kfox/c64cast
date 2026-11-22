- **mhires video shimmers less on flat areas.** Grain on a wall or a sky that
  sits between two palette colors no longer churns into pools of shifting
  color: a pixel on a flat area now keeps its color up to four times as firmly
  as one on an edge, so edges still follow motion. A slow fade across a flat
  area can trail the source for a second or two. The new
  `[color].flat_stickiness` setting, on by default, turns it off.
