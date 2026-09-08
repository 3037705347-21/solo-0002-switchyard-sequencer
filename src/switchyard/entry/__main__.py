"""Allow `python -m switchyard.entry` to start the service."""

from .cli import main

raise SystemExit(main())
