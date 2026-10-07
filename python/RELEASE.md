# Releasing uselayer

The owner publishes from their own Terminal (PyPI needs 2FA). A session prepares everything up to
the upload and stops.

## Before 0.1.0 is published

- [ ] The owner has read Polymarket US's Participant Agreement and Rulebook (Polymarket US has no
      separate developer terms).
- [ ] The README has been reviewed by the owner.
- [ ] The owner has run the Polymarket US smoke test with their own key and it passed:
      `POLYMARKET_US_KEY_ID=... POLYMARKET_US_SECRET_KEY=... python scripts/smoke_polymarket_us.py --yes`

## Every release

1. [ ] `fee-golden.json` is current: copy `sdk/fee-golden.json` from Layer's repo (written by
       `npx tsx scripts/export-fee-golden.ts` there). CI fails if the SDK has a fee schedule newer than
       the golden file.
2. [ ] Version bumped in `python/pyproject.toml` and `python/src/uselayer/_version.py`.
3. [ ] `python/CHANGELOG.md` has the release date and its changes.
4. [ ] `_switches.py` trades only the venues this release is allowed to trade.
5. [ ] CI is green on `main`.
6. [ ] From a clean checkout of the tagged commit (needs `uv`: `brew install uv`):

   ```bash
   cd python
   uv build
   uv publish        # asks for a PyPI token scoped to the uselayer project
   ```

7. [ ] Check it installs: `pip install uselayer==<version>` in a fresh virtualenv, then run
       `python examples/01_book_and_preview.py`.
8. [ ] Tag the commit: `git tag python-v<version> && git push --tags`.
