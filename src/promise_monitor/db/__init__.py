"""Database package. Importing it registers every model on ``Base.metadata`` (for Alembic)."""

from promise_monitor.db import models as models
