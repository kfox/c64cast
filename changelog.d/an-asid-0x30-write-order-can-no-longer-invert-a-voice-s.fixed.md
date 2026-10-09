- **An ASID `0x30` write order can no longer invert a voice's hard restart.**
  A hard restart is two writes to one control register — gate off, then the
  re-attack — and the buffered player ordered a frame's writes by ASID register
  id. A recipe that named the second control id (25-27, the ordinary ones) but
  not the first emitted the re-attack at the recipe's position and the gate-off
  value after it, so the voice ended the frame gated off and never sounded. The
  order within a register is now the serializer's own property: the pair is
  positioned as a unit, whatever ids a recipe names or omits, and each write
  still takes the wait its own id was given.
