- **The appliance setup window reopened on any lost `setup.json`, not just
  `--reset-setup`.** Whether to serve the unauthenticated setup form was
  decided by the absence of one file under the *data* root, and that cannot
  tell "this is a first boot" from "this host lost its data dir": a data
  root that is a container layer with no volume, a tmpfs, or a swept cache
  reopened `POST /api/setup`, `/setup` and the whole console shell to
  everything on the segment — while the host stayed fully configured,
  because machine settings live under the *config* dir — and `console_mdns`
  announced it with `setup=1`. Whoever won that race could repoint the box's
  connection at a host they control and, on a host relying on its generated
  token, write a replacement admin credential and evict the operator's
  bookmarked link. The window now also requires that machine settings *not*
  already name a connection target; a provisioned host with no marker logs a
  warning and stays shut, and `c64cast --reset-setup` writes an explicit
  reopen marker (`<data root>/setup-reopen`) so an admin who already has
  shell access can still ask for the window while a lost data dir cannot ask
  for it by itself. Opening it is a `log.warning` either way.
