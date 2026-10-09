- **A `big_text` overlay no longer overwrites the REU audio pump's code, and
  `big_text` on a `blank` display is refused while `[audio].use_reu_pump` is
  on and the scene's audio could run it.** The overlay kept its two shadow registers at `$C100`/`$C101`, where the
  pump's interrupt handler starts, so every frame of the scroll rewrote the
  pump's first two instructions. The shadows now live at `$C01E`/`$C01F`, next
  to the overlay's own handler. On a `blank` display the overlay also takes the
  `$0314` IRQ vector and masks CIA #1, which is the pump's interrupt, so the
  pump stopped refilling and the scene replayed stale audio. The configuration
  is now refused at load with a message naming both settings, except on a
  backend with no REU (the pump is switched off there) and on a `blank`
  scene in an ensemble (which holds no audio). `big_text` on
  `mcm` hooks no interrupt and is unaffected.
