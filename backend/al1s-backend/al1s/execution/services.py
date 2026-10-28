"""Execution resource facade; each operation keeps its existing UnitOfWork boundary."""

from al1s.execution.resource_base import UowFactory as UowFactory
from al1s.execution.resource_identity import TerminalIdentityOperations
from al1s.execution.resource_targets import TargetDeviceOperations


class ExecutionResourceService(TerminalIdentityOperations, TargetDeviceOperations):
    """Terminal identity and target-device operations behind one API facade."""
