"""How a run's trigger is named where runs are listed.

The user, the API and the command line show as the name of who started the run, so only a run
that nobody deployed carries a label of its own.
"""

from app.models import DeployTrigger

COMPOSE_IMPORT_LABEL = "Imported from Compose"

_LABELS = {DeployTrigger.COMPOSE: COMPOSE_IMPORT_LABEL}


def trigger_label(trigger: DeployTrigger) -> str | None:
    """The label shown beside the run, None for a run started by a person, token or command."""
    return _LABELS.get(trigger)
