# Publishing

## Prerequisites

1. Create a separate public source repository, recommended name `GIGAParviz/crewmemory-mcp`.
   Do not use the Git-backed memory data repository for source code.
2. Push this source tree and enable GitHub Actions.
3. Create the `crewmemory-mcp` project on PyPI and configure Trusted Publishing for the source
   repository, workflow `release.yml`, and environment `pypi`.
4. In GitHub, create the `pypi` environment. No long-lived PyPI token is required.

## Release checklist

1. Update the version in `pyproject.toml`, `src/crewmemory/__init__.py`, and `CHANGELOG.md`.
2. Run the full test suite and build checks locally.
3. Push a version tag and publish a GitHub Release.
4. Confirm the `Publish to PyPI` workflow succeeds.
5. Test a clean `uvx --from crewmemory-mcp crewmemory --help` invocation.
6. Test one real client install, restart it, list MCP tools, call `team_context`, and write a note
   to a disposable private memory repository.

The PyPI project URL and GitHub source URL in `pyproject.toml` assume the recommended repository
name. Change them before release if a different source repository is used.
