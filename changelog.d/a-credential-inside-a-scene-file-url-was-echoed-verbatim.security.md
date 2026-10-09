- **A credential inside a scene `file =` URL was echoed verbatim.** A private
  asset is legitimately reached with
  `file = "https://user:token@cdn.example/clip.mp4"`, and nothing redacted it:
  a resolve failure quoted the spec into `--log-file` and into the report the
  web console renders in a browser, and `recording_metadata` copied it into the
  per-scene snapshot — which `scripts/scene_config_to_description.py` renders as
  `Source video: <url>` in a **published** video description. The connection
  target has refused a `user:pass@` netloc for exactly this reason since it was
  introduced; a secret inside a scene `file` *value* was covered by none of that
  machinery, because it is not a field of its own. Every message that quotes a
  media spec now strips URL userinfo and masks `token=`/`password=`-style query
  parameters, and so does the snapshot.
