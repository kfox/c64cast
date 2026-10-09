- An exported-but-empty `C64CAST_CONTROL_TOKEN` /
  `C64CAST_CONTROL_VIEWER_TOKEN` / `C64CAST_DMA_PASSWORD` blanked a
  configured value. `VAR=$UNSET_OTHER` in a service unit or `docker -e VAR`
  exports a string, not nothing, so the fold overwrote the token the config
  had legitimately set. Empty now counts as unset; to run with no token,
  leave the field empty and the variable unset.
