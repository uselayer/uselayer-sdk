# Releasing `uselayer`

Releases go to PyPI through Trusted Publishing: GitHub Actions proves to PyPI which workflow is running,
so no API token or password is kept anywhere.

1. Bump `src/uselayer/_version.py` and add a `## X.Y.Z` heading to `CHANGELOG.md`. Merge to `main`.
2. Tag and push from an up-to-date `main`:

   ```
   git checkout main && git pull --ff-only
   git tag python-vX.Y.Z && git push origin python-vX.Y.Z
   ```

3. The `Publish to PyPI` workflow checks that the tag, `_version.py` and the changelog agree, runs lint, type
   checks and tests, builds, and waits for approval on the `pypi` environment. Approve it in the Actions tab
   and it publishes.

If a run fails before publishing, fix it on `main`, delete and re-push the tag, or re-run the workflow by hand
(Actions → Publish to PyPI → Run workflow, with the tag as the ref).

## One-time setup (done once per project)

- PyPI → your projects → `uselayer` → Manage → Publishing → add a GitHub publisher: owner `Dave-56`,
  repository `uselayer-sdk`, workflow `publish.yml`, environment `pypi`.
- GitHub → `uselayer-sdk` → Settings → Environments → New environment `pypi`, with required reviewer `Dave-56`.
