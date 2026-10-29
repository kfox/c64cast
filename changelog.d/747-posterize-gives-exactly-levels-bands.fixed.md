- **The `posterize` effect now gives exactly `levels` bands, from black to
  white.** An odd level count produced one band too many (`levels = 3` gave
  four), and some counts, such as 8, stopped short of full white.
