"""VIC-II display mode renderers.

Each mode owns the conversion from an OpenCV BGR frame to the byte layout
the VIC-II expects, plus the register pokes needed to put the chip into
that mode. All renderers go through C64Backend.write_region so the
delta-upload cache can skip unchanged bytes.

Package layout: one module per mode (`petscii`/`blank`/`mcm`/`hires`/
`mhires`) over two mid-bases (`char`, `bitmap`) and the shared machinery +
`DisplayMode` base in `base`. The C64-side 6502 layer for the tear-free
bitmap pipelines lives one level up in `modes_irq.py`; the bitmap modes
here install and drive it per frame.

The live-tunable pick knobs (`PALETTE_PICK_EMA_ALPHA`, `PERCELL_*`) are
re-exported here as value snapshots; their *rebindable* home is
`modes.base`, which the mode classes read at call time — a diag that wants
to retune them at runtime must set them on `c64cast.modes.base`.
"""

from .base import (
    BG0_HYSTERESIS_MARGIN as BG0_HYSTERESIS_MARGIN,
)
from .base import (
    DEFAULT_SAT_FACTOR as DEFAULT_SAT_FACTOR,
)
from .base import (
    ERROR_MIN_POOL_SIZE as ERROR_MIN_POOL_SIZE,
)
from .base import (
    GRAYSCALE_MCM_BGS as GRAYSCALE_MCM_BGS,
)
from .base import (
    GRAYSCALE_MHIRES_SLOTS as GRAYSCALE_MHIRES_SLOTS,
)
from .base import (
    ORDERED_DITHER_OFFSET_FNS as ORDERED_DITHER_OFFSET_FNS,
)
from .base import (
    PALETTE_MODES as PALETTE_MODES,
)
from .base import (
    PALETTE_PICK_EMA_ALPHA as PALETTE_PICK_EMA_ALPHA,
)
from .base import (
    PERCELL_CODE_HYSTERESIS_BONUS as PERCELL_CODE_HYSTERESIS_BONUS,
)
from .base import (
    PERCELL_PICK_EMA_ALPHA as PERCELL_PICK_EMA_ALPHA,
)
from .base import (
    PERCELL_QUANT_HYSTERESIS_BONUS as PERCELL_QUANT_HYSTERESIS_BONUS,
)
from .base import (
    BitmapComposeBuffers as BitmapComposeBuffers,
)
from .base import (
    ComposeBuffers as ComposeBuffers,
)
from .base import (
    DisplayMode as DisplayMode,
)
from .base import (
    MCMComposeBuffers as MCMComposeBuffers,
)
from .base import (
    MHiresComposeBuffers as MHiresComposeBuffers,
)
from .base import (
    advance_palette_cycle as advance_palette_cycle,
)
from .base import (
    ema_counts as ema_counts,
)
from .base import (
    fade_nibbles as fade_nibbles,
)
from .base import (
    palette_mode_settings as palette_mode_settings,
)
from .base import (
    pick_cell_colors as pick_cell_colors,
)
from .base import (
    resolve_color_shaping as resolve_color_shaping,
)
from .base import (
    validate_cell_strategy as validate_cell_strategy,
)
from .base import (
    validate_palette_mode as validate_palette_mode,
)

# Import order below IS DisplayMode.__subclasses__() creation order — the
# historical modes.py declaration order. introspect's live-target walk, the
# MIDI-setup wizard's pick lists, and the generated reference appendix F all
# render in that order, so these lines must not be re-sorted.
# isort: off
from .char import (
    CharDisplayMode as CharDisplayMode,
    clear_char_screen as clear_char_screen,
)
from .bitmap import (
    BitmapDisplayMode as BitmapDisplayMode,
    engage_bitmap_mode as engage_bitmap_mode,
)
from .petscii import PETSCIIDisplayMode as PETSCIIDisplayMode
from .blank import BlankDisplayMode as BlankDisplayMode
from .mcm import MCMDisplayMode as MCMDisplayMode
from .hires import (
    HIRES_STYLES as HIRES_STYLES,
    HiresDisplayMode as HiresDisplayMode,
)
from .mhires import MultiHiresDisplayMode as MultiHiresDisplayMode
# isort: on
