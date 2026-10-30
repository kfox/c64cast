- **A web page on another site can no longer complete an appliance's setup.**
  While the `[web].setup_wizard` window was open, `POST /api/setup` accepted a
  cross-site `<form enctype="text/plain">` whose field carried the JSON, a
  request browsers send with no preflight. Any page an admin visited could set
  the console token and point the appliance at another host. The route now
  refuses a request whose `Origin` is not the host it reached (`403`) and a body
  that is not `application/json` (`415`), the same check the performance
  console's command route makes.
