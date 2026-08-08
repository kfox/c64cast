"""Live U64 hardware auto-provisioning over the Ultimate REST config API.

`cli.build_stack` calls `provision_reu`/`provision_sampler` on every run: when
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

from c64cast.config import Config

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
    Gated entirely here so `cli.build_stack` can call it unconditionally:

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
    failed or the response shape was unrecognized. Used by cli.build_stack to
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
# routing/level in "Audio Mixer". The presence of these config keys is how we
# detect that the firmware exposes the sampler at all (sampler.py).
_SAMPLER_MAP_CATEGORY = REU_CONFIG_CATEGORY  # "C64 and Cartridge Settings"
_SAMPLER_MAP_FIELD = "Map Ultimate Audio $DF20-DFFF"
_SAMPLER_MIXER_CATEGORY = "Audio Mixer"
_SAMPLER_VOL_FIELDS = ("Vol Sampler L", "Vol Sampler R")
# The mixer volume enum's audible "0 dB" label. The firmware's volumes[] table
# (u64_config.cc) stores it with a LEADING SPACE (" 0 dB", index 24); the REST
# GET returns it verbatim and the PUT expects the same label, so match it.
_SAMPLER_VOL_AUDIBLE = " 0 dB"
SAMPLER_VOL_OFF = "OFF"
# Composite restore-key separator: provision_sampler spans two config
# categories (map vs mixer), so the restore dict keys are "category\x1ffield".
_RESTORE_SEP = "\x1f"


def read_sampler_config(
    api: object,
) -> tuple[bool | None, bool | None, dict[str, str]]:
    """Read the U64's Ultimate Audio sampler state over REST.

    Returns ``(present, map_enabled, volumes)``:
      * ``present`` — True if the firmware exposes the sampler config keys (it
        has the feature), False if absent, None if the REST query failed.
      * ``map_enabled`` — the $DF20 I/O-map enable (None when not present).
      * ``volumes`` — current ``{field: value}`` for the Sampler mixer channels
        (for restore). Reuses `fetch_config_section` so it tracks firmware
        response-shape variants identically to the REU/SID probes."""
    cart, _d1, err1 = fetch_config_section(
        api, _SAMPLER_MAP_CATEGORY, field_hint=_SAMPLER_MAP_FIELD
    )
    mixer, _d2, err2 = fetch_config_section(
        api, _SAMPLER_MIXER_CATEGORY, field_hint=_SAMPLER_VOL_FIELDS[0]
    )
    if err1 is not None or err2 is not None:
        return None, None, {}
    map_raw = cart.get(_SAMPLER_MAP_FIELD)
    present = (map_raw is not None) and all(f in mixer for f in _SAMPLER_VOL_FIELDS)
    if not present:
        return False, None, {}
    volumes: dict[str, str] = {}
    for field in _SAMPLER_VOL_FIELDS:
        v = mixer.get(field)
        if isinstance(v, str):
            volumes[field] = v
    return True, (map_raw == "Enabled"), volumes


def sampler_is_available(api: object) -> bool | None:
    """True iff the firmware exposes the Ultimate Audio sampler AND it is
    currently usable (the $DF20 I/O map is enabled and at least one Sampler
    mixer channel is not OFF). None when the REST query failed; False when the
    feature is absent / mapped-off / muted.

    Used by `cli._resolve_sampler_available` to resolve [audio].backend — None
    or False degrades to the 4-bit DAC. Run AFTER `provision_sampler` so a box
    this run just enabled reads as available."""
    present, map_enabled, volumes = read_sampler_config(api)
    if present is None:
        return None
    if not present:
        return False
    audible = any(v != SAMPLER_VOL_OFF for v in volumes.values())
    return bool(map_enabled) and audible


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

    present, map_enabled, volumes = read_sampler_config(api)
    if present is None:
        log.warning(
            "sampler: config wants the Ultimate Audio sampler (%s) but its state "
            "could not be read — leaving it unchanged.",
            ", ".join(reasons),
        )
        return None
    if not present:
        # Firmware doesn't expose the sampler; resolve falls back to the DAC.
        return None

    restore: dict[str, str] = {}
    if not map_enabled:
        try:
            api.put_config_item(_SAMPLER_MAP_CATEGORY, _SAMPLER_MAP_FIELD, "Enabled")  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not enable %s over REST: %s", _SAMPLER_MAP_FIELD, e)
            return restore or None
        restore[f"{_SAMPLER_MAP_CATEGORY}{_RESTORE_SEP}{_SAMPLER_MAP_FIELD}"] = "Disabled"

    for fieldname, cur in volumes.items():
        if cur != SAMPLER_VOL_OFF:
            continue
        try:
            api.put_config_item(_SAMPLER_MIXER_CATEGORY, fieldname, _SAMPLER_VOL_AUDIBLE)  # type: ignore[attr-defined]
        except requests.RequestException as e:
            log.warning("sampler: could not unmute %s: %s", fieldname, e)
        else:
            restore[f"{_SAMPLER_MIXER_CATEGORY}{_RESTORE_SEP}{fieldname}"] = cur

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
