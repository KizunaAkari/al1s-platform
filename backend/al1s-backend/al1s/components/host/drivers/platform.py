from al1s.components.contracts import ComponentRelease
from al1s.components.host.backup import schema_revision
from al1s.components.host.compose_driver import ComposeDeployment


def migrate(deployment: ComposeDeployment, release: ComponentRelease) -> None:
    if release.data_policy == "alembic":
        if schema_revision(deployment) != release.schema_from:
            raise ValueError("platform_schema_changed")
        deployment.runner.run(
            [
                *deployment.settings.compose_arguments(),
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "migrate",
            ],
            600,
        )
        if schema_revision(deployment) != release.schema_to:
            raise ValueError("platform_migration_unconfirmed")
