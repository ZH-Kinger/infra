from __future__ import annotations

from .errors import OptionalDependencyError


def resolve_reference(
    repository: str,
    reference: str,
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
) -> str:
    """Resolve a branch/tag/ref expression to its immutable lakeFS commit ID."""
    try:
        import lakefs
        from lakefs.client import Client
    except ImportError as exc:
        raise OptionalDependencyError(
            "lakeFS reference resolution requires `pip install -e '.[lakefs]'`"
        ) from exc

    client = Client(
        host=endpoint,
        username=access_key_id,
        password=secret_access_key,
    )
    repo = lakefs.Repository(repository, client=client)
    commit_id = repo.ref(reference).get_commit().id
    if not commit_id:
        raise RuntimeError(f"lakeFS returned an empty commit for reference {reference!r}")
    return str(commit_id)
