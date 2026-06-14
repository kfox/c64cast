"""Built-in Orchestrator subclasses.

Importing this package triggers each subclass module's
`@register_orchestrator` decorator, so simply doing
`import c64cast.orchestrators` from cli.py at startup is enough to
make every ensemble effect discoverable via the registry. New
subclasses should be added here as a top-level import as they land."""

from . import big_text_span  # noqa: F401  (import triggers registration)

__all__ = ["big_text_span"]
