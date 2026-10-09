- `hw/c64.py`'s `cpu_clock`/`frame_rate`/`kernal_cia1_latch` silently treated
  any system string other than exactly `"NTSC"` as PAL — including the
  unresolved `"auto"` config default (which `config.SYSTEM_CHOICES`
  explicitly allows) and any typo or trailing whitespace — giving every
  clock-derived constant (CIA latch, NMI safety band, SID PLAY rate) the
  wrong standard's numbers with zero diagnostic; `hw/api.py` already
  hand-guarded one call site against exactly this. They now accept only
  `"NTSC"`/`"PAL"` (case-insensitive, whitespace-tolerant, matching every
  other consumer's own `.upper()` convention) and raise `ValueError`
  otherwise. `scene_factory.validate_nmi_sample_rate` and
  `doctor._validate_audio_nmi_rate` both run before hardware opens (so
  `[ultimate64].system` can still be the unresolved `"auto"` there) and now
  resolve it to NTSC first, matching that field's own documented fallback
  and `hw_provision.resolve_system`'s convention, instead of reaching the
  PAL branch by accident. `actual_rate_for_latch` now raises on a negative
  latch instead of a bare `ZeroDivisionError` on `latch == -1`. Two register
  annotations were also corrected (`VIC_BANK_0.BITMAP`'s `$D018` bitmap
  nibble is `8`, not `4`; `CPU.PORT_IO_OUT` = `$34` has CHAREN, bit 2, still
  set — LORAM=HIRAM=0 is what maps RAM instead of ROM/I/O), and
  `RASTER_VBLANK_LINE`'s comment no longer calls line 248 the start of
  vblank (it is the first line past the last badline — a narrower property
  that breaks if YSCROLL or the row count changes; vblank itself is lines
  ~300+ on PAL, ~13-40 on NTSC). New `tests/test_c64.py` pins the
  system-string handling and the two negative/zero-rate guards.
