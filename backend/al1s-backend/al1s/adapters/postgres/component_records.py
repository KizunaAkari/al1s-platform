from al1s.adapters.postgres.component_models import ComponentReleaseRow
from al1s.components.contracts import ComponentRelease


def release_record(row: ComponentReleaseRow) -> ComponentRelease:
    return ComponentRelease.model_validate(
        {
            "release_id": row.id,
            **{
                name: getattr(row, name)
                for name in ComponentRelease.model_fields
                if name != "release_id"
            },
        }
    )
