"""Strip the read-only fields that `register-task-definition` rejects.

`describe-task-definition` returns fields describing a *registered* revision —
its ARN, revision number, status, and the attributes ECS derived from it. None
of them are inputs, and passing them back is an error rather than a no-op, so
the deploy workflow filters them out before rendering the next revision.

Extracted from an inline heredoc because two services now need it, and a
rollout that silently skipped this for one of them would fail mid-deploy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

READ_ONLY = (
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: strip_task_definition.py <task-definition.json>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    definition = json.loads(path.read_text())
    for field in READ_ONLY:
        definition.pop(field, None)
    path.write_text(json.dumps(definition, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
