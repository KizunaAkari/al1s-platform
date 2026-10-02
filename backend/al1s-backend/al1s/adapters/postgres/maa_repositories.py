"""Compatibility import surface for focused Maa persistence modules."""

from al1s.adapters.postgres.maa_application_repository import (
    PostgresMaaApplicationRepository as PostgresMaaApplicationRepository,
)
from al1s.adapters.postgres.maa_import_repository import (
    PostgresMaaImportRepository as PostgresMaaImportRepository,
)
from al1s.adapters.postgres.maa_mutation_receipt_repository import (
    PostgresMaaMutationReceiptRepository as PostgresMaaMutationReceiptRepository,
)
from al1s.adapters.postgres.maa_qualification_repository import (
    PostgresMaaQualificationRepository as PostgresMaaQualificationRepository,
)
from al1s.adapters.postgres.maa_quick_test_repository import (
    PostgresMaaQuickTestSessionRepository as PostgresMaaQuickTestSessionRepository,
)
from al1s.adapters.postgres.maa_script_repository import (
    PostgresMaaScriptRepository as PostgresMaaScriptRepository,
)
from al1s.adapters.postgres.maa_script_version_repository import (
    PostgresMaaScriptVersionRepository as PostgresMaaScriptVersionRepository,
)
from al1s.adapters.postgres.maa_strategy_repository import (
    PostgresMaaStrategyRepository as PostgresMaaStrategyRepository,
)
from al1s.adapters.postgres.maa_strategy_version_repository import (
    PostgresMaaStrategyVersionRepository as PostgresMaaStrategyVersionRepository,
)
