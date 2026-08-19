"""Live U64 hardware auto-provisioning over the Ultimate REST config API.

`session.build_stack` calls `provision_reu`/`provision_sampler` on every run: when
the config needs a feature the firmware has switched off, they enable it LIVE
+ VOLATILE (never saved to flash, so even a missed restore reverts on
power-cycle) and hand back the originals for `restore_reu`/`restore_sampler`
at teardown. The read-side helpers resolve the "auto" settings
(`reu_is_enabled` for [video].use_reu_staged, `sampler_is_available` for
[audio].backend), and `wants_reu`/`wants_sampler` are the single statement of
which config shapes need each feature — doctor's REU/sampler probes import
them, so the `--doctor` report and the provisioner can never disagree about
what a run requires. This block lived in doctor.py until the name stopped
fitting: diagnostics observe, this module mutates the machine.

Everything is Ultimate-only and best-effort. Gates on `profile.supports_*`
make each entry point a no-op on backends without the feature (TeensyROM), a
`--skip-probe` run never writes config it couldn't first read back, and a
failed REST call logs + degrades instead of failing the run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import NamedTuple

from c64cast.app.config import Config

from .backend import SYSTEM_MODE_CATEGORY

log = logging.getLogger(__name__)


def fetch_config_section(
    api: object,
    category: str,
    *,
    field_hint: str,
) -> tuple[dict[str, object], object, Exception | None]:
    """GET /v1/configs/<category> from the Ultimate and normalize the reply
    to its settings dict. Returns (section, raw_data, None) on success —
    `section` is {} when the response shape is unrecognized, which each caller
    treats on its own (SID stays quiet, REU warns). Returns ({}, None, exc)
    when the REST query itself failed, so the caller can build a probe-
    specific warning.

    Firmware 3.x returns
        {category: {<setting>: <value>, ...}, "errors": []};
    older / variant firmwares may return the section dict directly or as a
    single-item list. `field_hint` (a field expected in the flat shape) lets
    us recognize the direct-dict variant. This normalizer is firmware-coupled
    — single-sourced here so a response-shape change is a one-place fix (it
    previously lived, identically, in both probes).
    """
    from urllib.parse import quote

    import requests

    try:
        # `api` is a real Ultimate64API; reuse its REST session + base URL.
        base_url = api.base_url  # type: ignore[attr-defined]
        session = api.session  # type: ignore[attr-defined]
        url = f"{base_url}/v1/configs/{quote(category)}"
        r = session.get(url, timeout=3.0)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return {}, None, e

    section: dict[str, object] = {}
    if isinstance(data, dict):
        nested = data.get(category)
        if isinstance(nested, dict):
            section = nested
        elif field_hint in data:
            section = data
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        section = data[0]
    return section, data, None


def wants_reu(cfg: Config) -> tuple[bool, list[str]]:
    """Return (wants_reu, list of reasons). Reasons name which config flags
    flipped the want, so the doctor message can point the user at the right
    place to either turn the REU on at the U64 or flip the flag off."""
    reasons: list[str] = []
    if cfg.audio.use_reu_pump:
        reasons.append("[audio].use_reu_pump = true")
    # Only an EXPLICIT `use_reu_staged = true` is a hard REU requirement. The
    # default "auto" is self-healing (config.resolve_use_reu_staged falls back
    # to host-DMA when REU is off), so it must NOT make the doctor demand REU —
    # `is True` excludes both the "auto" string and any other truthy value.
    if cfg.video.use_reu_staged is True:
        reasons.append("[video].use_reu_staged = true")
    # The Ultimate Audio sampler streams its PCM ring out of REU SDRAM, so a run
    # that will use it needs the REU enabled + sized. Provisioning it also makes
    # "auto" video resolve to the tear-free REU bank-swap path — and since the
    # sampler runs off the C64 bus with no $0314 IRQ, REU-staged video and the
    # sampler coexist cleanly (no NMI/IRQ contention). Forward ref to
    # wants_sampler (both are module-level; resolved at call time).
    wants_samp, _ = wants_sampler(cfg)
    if wants_samp:
        reasons.append("[audio].backend sampler (REU-backed PCM ring)")
    # A buffered ASID scene streams frame-slots out of a REU ring, so a run with
    # one (asid_buffered_player auto/on) needs the REU enabled + sized. "auto"
    # only turns on where an REU exists — and provision_reu is itself gated on
    # supports_reu — so both auto and on are a genuine want here (unlike video's
    # self-healing use_reu_staged = "auto").
    if any(s.type == "asid" and s.asid_buffered_player in ("auto", "on") for s in cfg.scenes):
        reasons.append("[[scenes]] asid with asid_buffered_player (REU ring player)")
    return bool(reasons), reasons


# The Ultimate REST API returns the "RAM Expansion Unit" setting under
# this category path. Both the U64 and U2+ use the same category name.
REU_CONFIG_CATEGORY = "C64 and Cartridge Settings"
REU_ENABLED_FIELD = "RAM Expansion Unit"
REU_SIZE_FIELD = "REU Size"

# The firmware's "REU Size" enum labels (1541ultimate software/io/c64/c64.cc
# reu_size[]) → capacity in bytes. Used to (a) decide whether the U64's current
# REU is large enough for c64cast's staged offsets and (b) pick the size to
# provision. c64cast's highest REU offset is the video staging region near
# 14 MB (modes_irq.REU_VIDEO_BITMAP_COLOR_BASE = $E13000); the audio mic ring sits
# near 1 MB. 16 MB covers every offset and is FPGA-backed (free), so the
# provisioner always sizes to the max when it enables the REU.
_REU_SIZE_BYTES: dict[str, int] = {
    "128 KB": 128 << 10,
    "256 KB": 256 << 10,
    "512 KB": 512 << 10,
    "1 MB": 1 << 20,
    "2 MB": 2 << 20,
    "4 MB": 4 << 20,
    "8 MB": 8 << 20,
    "16 MB": 16 << 20,
}
REU_PROVISION_SIZE = "16 MB"


def read_reu_config(api: object) -> tuple[bool | None, str | None]:
    """Read the U64's REU state over REST. Returns ``(enabled, size_label)``.

    ``enabled`` is True/False, or None when the query failed or the field was
    absent (an unrecognized firmware shape) — i.e. "can't tell". ``size_label``
    is the raw "REU Size" string (e.g. ``"2 MB"``) or None. Reuses the shared
    `fetch_config_section` normalizer so it tracks firmware response-shape
    variants identically to `reu_is_enabled`."""
    section, _data, err = fetch_config_section(
        api, REU_CONFIG_CATEGORY, field_hint=REU_ENABLED_FIELD
    )
    if err is not None or not section:
        return None, None
    enabled_raw = section.get(REU_ENABLED_FIELD)
    enabled = None if enabled_raw is None else (enabled_raw == "Enabled")
    size_raw = section.get(REU_SIZE_FIELD)
    size = size_raw if isinstance(size_raw, str) else None
    return enabled, size


def provision_reu(api: object, cfg: Config) -> dict[str, str] | None:
    """Auto-enable + size the U64 REU for a run that needs it — LIVE + VOLATILE.

    Returns the original ``{field: value}`` to hand back to `restore_reu` at
    teardown, or None when nothing was changed (so a no-op is cheap to detect).
    Gated entirely here so `session.build_stack` can call it unconditionally:

      * ``[ultimate64].auto_reu`` must be on (default true),
      * the backend must have an REU (``profile.supports_reu`` — Ultimate only),
      * a probe must be allowed (not ``--skip-probe`` — we never write config we
        can't first read back to restore),
      * the config must HARD-require the REU (`wants_reu`: ``use_reu_pump`` or
        an explicit ``use_reu_staged = true`` — the same condition that makes
        doctor's ``_probe_reu_status`` demand the REU). The default
        ``use_reu_staged = "auto"`` is left alone: it self-heals to the
        host-DMA double-buffer path (also tear-free) without mutating the
        user's machine config.

    Enables the REU if off and grows it to 16 MB if smaller. The change is NOT
    saved to flash, so it reverts on the next power-cycle even if teardown's
    restore never runs. Best-effort: a REST failure logs a warning and returns
    whatever was changed so far (so teardown still restores it)."""
    if not cfg.ultimate64.auto_reu:
        return None
    profile = getattr(api, "profile", None)
    if profile is None or not getattr(profile, "supports_reu", False):
        return None
    if cfg.debug.skip_probe:
        return None
    wants, reasons = wants_reu(cfg)
    if not wants:
        return None

    import requests

    enabled, cur_size = read_reu_config(api)
    if enabled is None:
        log.warning(
            "auto_reu: config needs the REU (%s) but the U64's REU state could "
            "not be read — leaving it unchanged.",
            ", ".join(reasons),
        )
        return None

    restore: dict[str, str] = {}
    if not enabled:
        try:
            api.put_config_item(REU_CONFIG_CATEGORY, REU_ENABLED_FIELD, "Enabled")  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("auto_reu: could not enable the U64 REU over REST: %s", e)
            return restore or None
        restore[REU_ENABLED_FIELD] = "Disabled"

    cur_bytes = _REU_SIZE_BYTES.get(cur_size or "", 0)
    if cur_bytes < _REU_SIZE_BYTES[REU_PROVISION_SIZE]:
        try:
            api.put_config_item(  # type: ignore[attr-defined]
                REU_CONFIG_CATEGORY, REU_SIZE_FIELD, REU_PROVISION_SIZE
            )
        except requests.RequestException as e:
            log.warning("auto_reu: could not set REU size to %s: %s", REU_PROVISION_SIZE, e)
        else:
            if cur_size is not None:
                restore[REU_SIZE_FIELD] = cur_size

    if restore:
        log.info(
            "auto_reu: U64 REU enabled (size %s) for this run (%s) — live, "
            "volatile (reverts on power-cycle), restored at teardown.",
            REU_PROVISION_SIZE,
            ", ".join(reasons),
        )
    return restore or None


def restore_reu(api: object, restore: dict[str, str] | None) -> None:
    """Put the REU config fields changed by `provision_reu` back to their
    original values (called once per stack at teardown). No-op when nothing was
    provisioned. Best-effort — a failed restore just logs (the change was
    volatile anyway, so a power-cycle clears it)."""
    if not restore:
        return

    import requests

    for fieldname, value in restore.items():
        try:
            api.put_config_item(REU_CONFIG_CATEGORY, fieldname, value)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("auto_reu: could not restore U64 %s = %s: %s", fieldname, value, e)
        else:
            log.info("auto_reu: restored U64 %s = %s", fieldname, value)


def reu_is_enabled(api: object) -> bool | None:
    """Query the Ultimate's REU enable state over REST.

    Returns True/False when the firmware reports it, or None when the query
    failed or the response shape was unrecognized. Used by session.build_stack to
    resolve the [video].use_reu_staged "auto" setting — a None (can't tell) is
    treated as "not available" there so auto degrades to host-DMA rather than
    staging into a REU that might be off (which would silently freeze video)."""
    section, _data, err = fetch_config_section(
        api, REU_CONFIG_CATEGORY, field_hint=REU_ENABLED_FIELD
    )
    if err is not None or not section:
        return None
    return section.get(REU_ENABLED_FIELD) == "Enabled"


# ---- Ultimate Audio FPGA PCM sampler ($DF20-$DFFF) ----------------------
# The $DF20 I/O map lives in "C64 and Cartridge Settings"; the stereo mixer
# routing/level in a mixer category. The presence of these config keys is how
# we detect that the firmware exposes the sampler at all (sampler.py).
_SAMPLER_MAP_CATEGORY = REU_CONFIG_CATEGORY  # "C64 and Cartridge Settings"
_SAMPLER_MAP_FIELD = "Map Ultimate Audio $DF20-DFFF"
# The category carrying the "Vol Sampler L/R" channels differs across the
# Ultimate family: the U64 has a dedicated "Audio Mixer"; the Ultimate II+
# (firmware 3.x) folds the same fields into "Audio Output Settings". Probed in
# order — first category actually carrying the fields wins — because the
# firmware answers a GET for a category it doesn't have with HTTP 200 and an
# empty body rather than an error, so a single fixed name would silently read
# "sampler absent" on the other device.
_SAMPLER_MIXER_CATEGORIES = ("Audio Mixer", "Audio Output Settings")
_SAMPLER_VOL_FIELDS = ("Vol Sampler L", "Vol Sampler R")
# The mixer volume enum's audible "0 dB" label. The firmware's volumes[] table
# (u64_config.cc) stores it with a LEADING SPACE (" 0 dB", index 24); the REST
# GET returns it verbatim and the PUT expects the same label, so match it.
_SAMPLER_VOL_AUDIBLE = " 0 dB"
SAMPLER_VOL_OFF = "OFF"
# Composite restore-key separator: provision_sampler spans two config
# categories (map vs mixer), so the restore dict keys are "category\x1ffield".
_RESTORE_SEP = "\x1f"


class SamplerConfig(NamedTuple):
    """Live Ultimate Audio sampler state (see :func:`read_sampler_config`)."""

    present: bool | None
    map_enabled: bool | None
    volumes: dict[str, str]
    mixer_category: str | None


def _read_sampler_mixer(api: object) -> tuple[str | None, dict[str, str], bool]:
    """The (category, volumes) of the first candidate mixer category carrying
    the Sampler channels, or ``(None, {}, any_read_failed)`` when none does."""
    any_read_failed = False
    for category in _SAMPLER_MIXER_CATEGORIES:
        mixer, _data, err = fetch_config_section(api, category, field_hint=_SAMPLER_VOL_FIELDS[0])
        if err is not None:
            any_read_failed = True
            continue
        if all(f in mixer for f in _SAMPLER_VOL_FIELDS):
            volumes = {f: v for f in _SAMPLER_VOL_FIELDS if isinstance(v := mixer.get(f), str)}
            return category, volumes, False
    return None, {}, any_read_failed


def read_sampler_config(api: object) -> SamplerConfig:
    """Read the Ultimate Audio sampler state over REST.

    * ``present`` — True if the firmware exposes the sampler config keys (it
      has the feature), False if absent, None if a REST query failed.
    * ``map_enabled`` — the $DF20 I/O-map enable (None when not present).
    * ``volumes`` — current ``{field: value}`` for the Sampler mixer channels
      (for restore). Reuses `fetch_config_section` so it tracks firmware
      response-shape variants identically to the REU/SID probes.
    * ``mixer_category`` — the category carrying those channels on this
      device (see ``_SAMPLER_MIXER_CATEGORIES``); the target every mixer PUT
      and composite restore key must use."""
    cart, _d1, err1 = fetch_config_section(
        api, _SAMPLER_MAP_CATEGORY, field_hint=_SAMPLER_MAP_FIELD
    )
    mixer_category, volumes, mixer_read_failed = _read_sampler_mixer(api)
    if err1 is not None or (mixer_category is None and mixer_read_failed):
        return SamplerConfig(None, None, {}, None)
    map_raw = cart.get(_SAMPLER_MAP_FIELD)
    if map_raw is None or mixer_category is None:
        return SamplerConfig(False, None, {}, None)
    return SamplerConfig(True, map_raw == "Enabled", volumes, mixer_category)


def sampler_is_available(api: object) -> bool | None:
    """True iff the firmware exposes the Ultimate Audio sampler AND it is
    currently usable (the $DF20 I/O map is enabled and at least one Sampler
    mixer channel is not OFF). None when the REST query failed; False when the
    feature is absent / mapped-off / muted.

    Used by `session._resolve_sampler_available` to resolve [audio].backend — None
    or False degrades to the 4-bit DAC. Run AFTER `provision_sampler` so a box
    this run just enabled reads as available."""
    state = read_sampler_config(api)
    if state.present is None:
        return None
    if not state.present:
        return False
    audible = any(v != SAMPLER_VOL_OFF for v in state.volumes.values())
    return bool(state.map_enabled) and audible


def wants_sampler(cfg: Config) -> tuple[bool, list[str]]:
    """Return (wants_sampler, reasons). The run wants the sampler when audio is
    enabled, [audio].backend is auto/sampler (not the forced DAC), and a scene is
    wired to play through it: a ``video`` scene, or a ``generative`` scene with
    ``audio_source = "file"`` (a decoded track — the DAC path is staticky, so it
    routes through the sampler too). Provisioning uses this to enable the FPGA
    map, so missing a sampler-routed scene here leaves it silent."""
    reasons: list[str] = []
    if not cfg.audio.enabled:
        return False, reasons
    backend = cfg.audio.backend
    if backend not in ("auto", "sampler"):
        return False, reasons
    if any(s.type == "video" for s in cfg.scenes):
        reasons.append(f"[audio].backend = {backend!r} + video scene(s)")
    if any(s.type == "generative" and s.audio_source == "file" for s in cfg.scenes):
        reasons.append(f'[audio].backend = {backend!r} + generative audio_source="file" scene(s)')
    return bool(reasons), reasons


def provision_sampler(api: object, cfg: Config) -> dict[str, str] | None:
    """Auto-enable the Ultimate Audio sampler for a run that will use it —
    LIVE + VOLATILE (mirrors `provision_reu`). Enables the $DF20 I/O map if off
    and unmutes the Sampler mixer channels if OFF, capturing the originals for
    `restore_sampler` at teardown. Returns the restore dict (composite keys
    ``"category\\x1ffield" -> original``) or None when nothing was changed.

    Gated on ``profile.supports_sampler`` + not ``--skip-probe`` + `wants_sampler`.
    The change is NOT saved to flash, so it reverts on power-cycle even if the
    restore is missed. Best-effort: a REST failure logs and returns what changed
    so far (so teardown still restores it)."""
    profile = getattr(api, "profile", None)
    if profile is None or not getattr(profile, "supports_sampler", False):
        return None
    if cfg.debug.skip_probe:
        return None
    wants, reasons = wants_sampler(cfg)
    if not wants:
        return None

    import requests

    state = read_sampler_config(api)
    if state.present is None:
        log.warning(
            "sampler: config wants the Ultimate Audio sampler (%s) but its state "
            "could not be read — leaving it unchanged.",
            ", ".join(reasons),
        )
        return None
    if not state.present or state.mixer_category is None:
        # Firmware doesn't expose the sampler; resolve falls back to the DAC.
        return None

    restore: dict[str, str] = {}
    if not state.map_enabled:
        try:
            api.put_config_item(_SAMPLER_MAP_CATEGORY, _SAMPLER_MAP_FIELD, "Enabled")  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not enable %s over REST: %s", _SAMPLER_MAP_FIELD, e)
            return restore or None
        restore[f"{_SAMPLER_MAP_CATEGORY}{_RESTORE_SEP}{_SAMPLER_MAP_FIELD}"] = "Disabled"

    for fieldname, cur in state.volumes.items():
        if cur != SAMPLER_VOL_OFF:
            continue
        try:
            api.put_config_item(state.mixer_category, fieldname, _SAMPLER_VOL_AUDIBLE)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not unmute %s: %s", fieldname, e)
        else:
            restore[f"{state.mixer_category}{_RESTORE_SEP}{fieldname}"] = cur

    if restore:
        log.info(
            "sampler: Ultimate Audio enabled for this run (%s) — live, volatile "
            "(reverts on power-cycle), restored at teardown.",
            ", ".join(reasons),
        )
    return restore or None


def restore_sampler(api: object, restore: dict[str, str] | None) -> None:
    """Put the sampler config fields changed by `provision_sampler` back to
    their originals at teardown. No-op when nothing was provisioned. Best-effort
    — a failed restore just logs (the change was volatile anyway)."""
    if not restore:
        return

    import requests

    for key, value in restore.items():
        category, _, fieldname = key.partition(_RESTORE_SEP)
        try:
            api.put_config_item(category, fieldname, value)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not restore %s = %s: %s", fieldname, value, e)
        else:
            log.info("sampler: restored %s = %s", fieldname, value)


# ---- System Mode (PAL/NTSC machine timing) --------------------------------
# The Ultimate's "System Mode" enum names look like they select a video
# standard. They do not: the *suffix* selects the machine timing and the
# *prefix* selects only the analog chroma encoding. From the firmware's
# timing table (1541ultimate software/u64/color_timings.cc), cross-checked by
# measuring phi2 + the VIC raster line count on a real unit:
#
#   PAL, NTSC-50, NTSC-50/L  -> 63 cycles/line, 312 lines, phi2 985248  (PAL)
#   NTSC, PAL-60, PAL-60/L   -> 65 cycles/line, 263 lines, phi2 1022727 (NTSC)
#
# So "NTSC-50" is a PAL-timed machine emitting NTSC color, and "PAL-60" is an
# NTSC-timed machine emitting PAL color. Over HDMI the pairs are
# indistinguishable. The "/L" variants differ only in the color-burst phase
# table (~0.1% fast) and matter to analog output alone.
#
# The mapping therefore looks backwards on purpose — do not "fix" it.
SYSTEM_MODE_FIELD = "System Mode"

SYSTEM_MODE_TIMING: dict[str, str] = {
    "PAL": "PAL",
    "NTSC-50": "PAL",
    "NTSC-50/L": "PAL",
    "NTSC": "NTSC",
    "PAL-60": "NTSC",
    "PAL-60/L": "NTSC",
}

# Which System Mode to select for a target timing, by analog chroma preference.
# Keyed (timing, chroma) -> firmware label. Over HDMI both columns are the same
# picture; the choice only matters on the composite/S-Video output.
#
# On composite the four hybrids are the classic non-standard combinations
# (color_timings.cc): "PAL-60" is PAL chroma at a 60 Hz field rate (c_pal_60_*,
# no VIDEO_FMT_NTSC_ENCODING), "NTSC-50" is NTSC chroma at 50 Hz
# (c_ntsc_50_*). Preserving the chroma keeps a set able to DECODE COLOR, but
# the field rate still changes underneath it — a single-standard analog display
# may simply not lock. PAL-60 is the more widely tolerated of the two.
SYSTEM_MODE_FOR: dict[tuple[str, str], str] = {
    ("PAL", "pal"): "PAL",
    ("PAL", "ntsc"): "NTSC-50",
    ("NTSC", "pal"): "PAL-60",
    ("NTSC", "ntsc"): "NTSC",
}

# The "/L" hybrids lock the color subcarrier to an exact line ratio instead of
# free-running it, and the firmware calls them the "best timing match to
# original C64" — c_pal_60_281_5 / c_ntsc_50_228_5 carry slightly LONGER
# periods (84422 / 81385) than their free-running siblings (84372 / 81300), so
# it is the PLAIN hybrids that run ~0.1% fast, not the locked ones.
#
# There is deliberately no attempt to carry a /L choice across a retime: every
# /L mode IS a hybrid, and retiming a hybrid always lands on the other
# standard's plain mode, which has no locked form ("PAL" and "NTSC" are already
# standard-locked). A composite user who picked /L for its accuracy therefore
# loses it for the duration of a `sid_video_mode` run and gets it back at
# teardown — one more reason that setting is opt-in.

# The HDMI upscaler, in the same category. Present only on the newer U64 board
# (firmware u64_config.cc guards it behind `#if U64 == 2`), so the read has to
# tolerate it being absent rather than assume it.
#
# It matters here because of how capture devices behave, not how the C64 does:
# at SD, PAL timing puts 576p50 on the wire, and some HDMI capture devices
# cannot lock to it — the picture tears or rolls. The same machine upscaled to
# 720p50/1080p50 captures cleanly on the same device (HW-verified on two
# different capture devices, which disagreed at SD and agreed at HD).
HDMI_RESOLUTION_FIELD = "HDMI Scan Resolution"

# The Ultimate's loaded palette file, in the same category as System Mode.
# Empty means the firmware is driving its built-in table (which is what
# palette.U64_PALETTE_BGR transcribes); non-empty names a .vpl the user loaded
# onto the machine, whose contents live in the Ultimate's own flash and are not
# reachable over the REST API.
PALETTE_FIELD = "Palette Definition"
HDMI_RESOLUTION_SD = "SD (480p/576p)"
# What "auto" raises SD to. 720p50 rather than 1080p50: it is the lower of the
# two HW-verified modes, so it asks less of both the upscaler and the capture
# card. The four "PC" modes are exposed but NOT verified under PAL timing.
HDMI_RESOLUTION_AUTO_TARGET = "HD (720p)"
# The firmware's scan_modes[] labels, in order (u64_config.cc).
HDMI_RESOLUTION_CHOICES: tuple[str, ...] = (
    HDMI_RESOLUTION_SD,
    "HD (720p)",
    "FullHD (1080p)",
    "PC 800 x 600",
    "PC 1024 x 768",
    "PC 1280 x 1024",
)

# Escape hatch, worth naming wherever we tell a user we changed their video
# mode: holding C= plus P (PAL) or N (NTSC) at Ultimate boot forces System Mode
# back, for a display or capture device that can't show what it was set to. The
# firmware scans the keyboard once during configurator init and overrides
# CFG_SYSTEM_MODE (u64_config.cc: key 0x10 -> index 0 = PAL, 0x0E -> index 1 =
# NTSC). CTRL works identically — keyboard_c64.cc's modifier_map gives C= and
# CTRL distinct bits, but keymaps[] points both at the same keymap_control
# table, which is where those two codes come from.
#
# It resets ONLY System Mode, not the scan resolution — but every write this
# module makes is volatile, so a power-cycle clears those regardless.
SYSTEM_MODE_BOOT_OVERRIDE_HINT = (
    "If a video mode leaves you with no picture, hold C= and P (PAL) or C= and "
    "N (NTSC) at Ultimate boot to force System Mode back; c64cast's changes are "
    "volatile and clear on a power-cycle either way."
)


def read_system_mode(api: object) -> str | None:
    """Read the Ultimate's live "System Mode" label (e.g. ``"NTSC-50"``), or
    None when it can't be read (see `read_video_output`)."""
    return read_video_output(api)[0]


def read_system_timing(api: object) -> str | None:
    """The machine's *timing* standard (``"PAL"``/``"NTSC"``) from its live
    System Mode, or None when it can't be read or the label is unknown to
    `SYSTEM_MODE_TIMING` (a firmware that grew a new mode — better to fall back
    to the configured value than to guess)."""
    label = read_system_mode(api)
    if label is None:
        return None
    timing = SYSTEM_MODE_TIMING.get(label)
    if timing is None:
        log.warning(
            "system: unrecognized System Mode %r — cannot derive PAL/NTSC timing "
            "from it. Set [ultimate64].system explicitly.",
            label,
        )
    return timing


def read_video_output(api: object) -> tuple[str | None, str | None]:
    """Read the Ultimate's live ``(System Mode, HDMI Scan Resolution)`` labels.

    Either element is None when it can't be read: the whole category is absent
    (the Ultimate II+ has none), the query failed, or — for the scan resolution
    specifically — the board is an older U64 whose firmware doesn't register
    that field at all. One GET covers both; they share a category."""
    section, _data, err = fetch_config_section(
        api, SYSTEM_MODE_CATEGORY, field_hint=SYSTEM_MODE_FIELD
    )
    if err is not None or not section:
        return None, None
    mode = section.get(SYSTEM_MODE_FIELD)
    res = section.get(HDMI_RESOLUTION_FIELD)
    return (
        mode if isinstance(mode, str) else None,
        res if isinstance(res, str) else None,
    )


def provision_video_output(api: object, cfg: Config) -> dict[str, str] | None:
    """Set the Ultimate's video output up for this run — LIVE + VOLATILE.

    Two related fields, one category, one restore dict:

      * **System Mode** (opt-in, ``[ultimate64].sid_video_mode``) — retimes the
        machine so its PAL/NTSC standard matches ``[ultimate64].system``. That
        corrects SID *pitch*, since phi2 differs 3.8% between the standards.
        Playback *tempo* is a separate lever needing no video change at all —
        see `[ultimate64].sid_play_rate` and
        `c64cast.hw.api.Ultimate64API.run_sid_player`.
      * **HDMI Scan Resolution** (``[ultimate64].hdmi_scan_resolution``) — the
        upscaler. Its default, ``"auto"``, exists to clean up after the switch
        above: PAL timing at SD puts 576p50 on the wire, which some capture
        devices cannot lock to, and the same machine at 720p50 captures fine.
        So "auto" raises SD to HD *only when this function also changed the
        timing* — c64cast fixes what it broke and leaves a machine it didn't
        retime alone. ``"keep"`` never touches it; an explicit label sets it
        for the run regardless.

    Returns the original ``{field: value}`` for `restore_video_output`, or None
    when nothing changed. Gated so `session.build_stack` can call it
    unconditionally: the backend must expose the category
    (`profile.supports_system_mode` — Ultimate 64 only) and a probe must be
    allowed (never write config we can't first read back).

    The System Mode change retunes the HDMI output, so it is resolved once per
    run rather than per scene: every switch costs the capture device a re-lock.
    Callers reset the C64 afterwards so the KERNAL re-runs its PAL/NTSC
    autodetect against the new timing. Best-effort throughout — a failed REST
    call logs and returns whatever was changed so far, so teardown still
    restores it."""
    profile = getattr(api, "profile", None)
    if profile is None or not getattr(profile, "supports_system_mode", False):
        return None
    if cfg.debug.skip_probe:
        return None
    want_res = cfg.ultimate64.hdmi_scan_resolution
    if cfg.ultimate64.sid_video_mode == "off" and want_res in ("auto", "keep"):
        return None

    import requests

    current_mode, current_res = read_video_output(api)
    if current_mode is None:
        log.warning(
            "sid_video_mode: the Ultimate's video output config could not be "
            "read — leaving it unchanged."
        )
        return None

    restore: dict[str, str] = {}
    retimed = False
    if cfg.ultimate64.sid_video_mode != "off":
        retimed = _switch_system_mode(api, cfg, current_mode, restore)

    # Raise SD only when we just retimed the machine (see the docstring); an
    # explicit label applies either way.
    target_res = want_res
    if want_res == "auto":
        target_res = (
            HDMI_RESOLUTION_AUTO_TARGET if retimed and current_res == HDMI_RESOLUTION_SD else "keep"
        )
    if target_res != "keep" and current_res is not None and target_res != current_res:
        try:
            api.put_config_item(SYSTEM_MODE_CATEGORY, HDMI_RESOLUTION_FIELD, target_res)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("hdmi_scan_resolution: could not set %s: %s", target_res, e)
        else:
            restore[HDMI_RESOLUTION_FIELD] = current_res
            log.info(
                "hdmi_scan_resolution: %s -> %s%s — live, volatile, restored at teardown.",
                current_res,
                target_res,
                " (SD at this timing is what some capture devices fail to lock to)"
                if want_res == "auto"
                else "",
            )
    return restore or None


def _switch_system_mode(api: object, cfg: Config, current: str, restore: dict[str, str]) -> bool:
    """Apply the System Mode half of `provision_video_output`. Records the
    original in `restore` and returns True when the machine was actually
    retimed."""
    import requests

    cur_timing = SYSTEM_MODE_TIMING.get(current)
    if cur_timing is None:
        return False
    want_timing = "NTSC" if cfg.ultimate64.system.upper() == "NTSC" else "PAL"
    if cur_timing == want_timing:
        return False
    # Keep the analog chroma encoding the machine is already set for — a user
    # on composite has chosen it deliberately, and over HDMI it makes no
    # difference either way.
    chroma = "ntsc" if current.startswith("NTSC") else "pal"
    target = SYSTEM_MODE_FOR[(want_timing, chroma)]
    try:
        api.put_config_item(SYSTEM_MODE_CATEGORY, SYSTEM_MODE_FIELD, target)  # type: ignore[attr-defined]
    except requests.RequestException as e:
        log.warning("sid_video_mode: could not set System Mode to %s: %s", target, e)
        return False
    restore[SYSTEM_MODE_FIELD] = current
    log.info(
        "sid_video_mode: System Mode %s -> %s (%s timing) for this run — live, "
        "volatile (reverts on power-cycle), restored at teardown. The HDMI "
        "output mode changes with it; your capture device has to re-lock. %s",
        current,
        target,
        want_timing,
        SYSTEM_MODE_BOOT_OVERRIDE_HINT,
    )
    return True


def restore_video_output(api: object, restore: dict[str, str] | None) -> None:
    """Put the video-output fields changed by `provision_video_output` back at
    teardown. No-op when nothing was provisioned. Best-effort — a failed restore
    just logs (the change was volatile anyway, so a power-cycle clears it)."""
    if not restore:
        return

    import requests

    for fieldname, value in restore.items():
        try:
            api.put_config_item(SYSTEM_MODE_CATEGORY, fieldname, value)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("video output: could not restore U64 %s = %s: %s", fieldname, value, e)
        else:
            log.info("video output: restored U64 %s = %s", fieldname, value)


def resolve_system(cfg: Config, api: object) -> None:
    """Settle `[ultimate64].system = "auto"` against the machine's live System
    Mode, and fold the result back into the already-built hardware profile.

    Must run after the backend exists rather than inside `make_backend`: the
    profile bakes `default_fps` + `host_sid_model` from the system at
    construction time, which is before there is any API to ask. So those fields
    are rebuilt in place once the answer is known.

    An explicitly configured system always wins — it stays the way to describe
    a machine the probe can't read (a TeensyROM-driven C64, `--skip-probe`) —
    but it is checked against the live mode and a disagreement is warned about,
    because every timing constant in the run derives from this one field.
    """
    import dataclasses

    from .backend import resolve_host_sid_model

    profile = getattr(api, "profile", None)
    configured = cfg.ultimate64.system
    live = (
        read_system_timing(api)
        if profile is not None
        and not cfg.debug.skip_probe
        and getattr(profile, "supports_system_mode", False)
        else None
    )
    if configured == "auto":
        if live is None:
            log.info(
                "[ultimate64].system = auto: this backend can't report its "
                "PAL/NTSC timing%s — assuming NTSC. Set it explicitly if that's "
                "wrong (every frame-rate and clock constant depends on it).",
                " (--skip-probe)" if cfg.debug.skip_probe else "",
            )
        else:
            log.info("[ultimate64].system = auto -> %s (read from the machine)", live)
        cfg.ultimate64.system = live or "NTSC"
    elif live is not None and live != configured.upper():
        log.warning(
            "[ultimate64].system = %s but the machine is running %s timing. "
            "Frame rate, CPU clock and SID PLAY rate will all be computed for "
            "the wrong standard. Use 'auto', or fix one of the two.",
            configured,
            live,
        )

    if profile is None:
        return
    # The profile was built from the pre-resolution value; rebuild what derives
    # from it.
    system = cfg.ultimate64.system
    host_model, host_model_assumed = resolve_host_sid_model(cfg.hardware.host_sid_model, system)
    if profile.host_sid_chips:
        host_model_assumed = False
    api.profile = dataclasses.replace(  # type: ignore[attr-defined]
        profile,
        system=system,
        default_fps=60.0 if system == "NTSC" else 50.0,
        host_sid_model=host_model,
        host_sid_model_assumed=host_model_assumed,
    )


def read_palette_definition(api: object) -> str | None:
    """The Ultimate's loaded palette filename, ``""`` for its built-in table, or
    None when the field can't be read (not a U64, query failed, older firmware).

    Distinguishing ``""`` from None is the point: the empty string is a positive
    answer that the firmware palette is in effect, None is no answer at all.
    """
    section, _data, err = fetch_config_section(
        api, SYSTEM_MODE_CATEGORY, field_hint=SYSTEM_MODE_FIELD
    )
    if err is not None or not section:
        return None
    value = section.get(PALETTE_FIELD)
    return value if isinstance(value, str) else None


def resolve_palette(cfg: Config, api: object) -> None:
    """Settle `[hardware].host_palette` and point the render pipeline at the
    colors this machine emits.

    Runs from the same place as `resolve_system` and for the same reason: what
    the machine reports about itself can only be read once the backend exists.
    Everything downstream reads the palette through
    :mod:`c64cast.video.palette`, so this is the one place that has to get it
    right — quantization, dither error diffusion, fades, and flicker-pair
    eligibility all measure distances against it.

    A configured value always wins, and is the only way to describe a machine
    that can't answer: a real C64 behind a TeensyROM+, or an Ultimate carrying a
    custom .vpl (whose contents live in the machine's flash, out of REST's
    reach — so point host_palette at a local copy of the same file).
    """
    from c64cast.video.palette import active_host_palette_name, set_host_palette

    global _palette_resolved
    name, table = _resolve_palette_table(cfg, api)
    if _palette_resolved:
        # The active palette is process-wide (see `palette.set_host_palette`),
        # so an ensemble of machines that render the 16 colors differently can
        # only be right about one of them. Say which, rather than letting the
        # second machine quietly inherit the first machine's colors.
        active = active_host_palette_name()
        if active != name:
            log.warning(
                "ensemble: this system's palette (%s) differs from the one "
                "already in effect (%s), and the color pipeline holds one "
                "palette for the whole process — keeping %s, so colors are "
                "matched against the wrong 16 on the other machine.",
                name,
                active,
                active,
            )
        return
    _palette_resolved = True
    set_host_palette(table, name=name)


def _resolve_palette_table(cfg: Config, api: object) -> tuple[str, Sequence[Sequence[int]]]:
    """``(name, BGR table)`` for this machine — see `resolve_palette`."""
    from c64cast.video.palette import HOST_PALETTES, resolve_host_palette

    configured = cfg.hardware.host_palette
    if configured != "auto":
        log.info("[hardware].host_palette = %s", configured)
        return configured, resolve_host_palette(configured)

    profile = getattr(api, "profile", None)
    is_u64 = profile is not None and getattr(profile, "supports_system_mode", False)
    if not is_u64 or cfg.debug.skip_probe:
        # Every other machine in reach is a real C64 — the Ultimate II+ and the
        # TeensyROM+ both drive one, and neither has a palette of its own.
        log.debug("[hardware].host_palette = auto -> pepto (real VIC-II assumed)")
        return "pepto", HOST_PALETTES["pepto"]

    loaded = read_palette_definition(api)
    if loaded:
        log.warning(
            "[hardware].host_palette = auto: this Ultimate has the custom "
            "palette %r loaded, which it won't serve over the network — "
            "assuming the built-in table instead, so colors will be matched "
            "against the wrong 16. Point host_palette at a local copy of that "
            ".vpl to fix it.",
            loaded,
        )
    log.info("[hardware].host_palette = auto -> u64 (read from the machine)")
    return "u64", HOST_PALETTES["u64"]


# Whether resolve_palette has already set the process-wide palette this run.
_palette_resolved = False
