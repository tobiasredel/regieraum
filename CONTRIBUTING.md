# Contributing to Regieraum

Thanks for helping. Bug reports, small fixes and documentation are all welcome. For larger changes please open an
issue first so we can agree on the direction. `docs/SPEC.md` (German) is the specification the code follows: data
model, protocol, audio pipeline and prompts. The project has four parts that only talk through narrow interfaces:
`app/` (server and web interface), `pipeline/` (audio processing), `publish/` (storage, feed, import, show notes) and
`assist/` (dossiers and live companion).

## Development setup

No Python on the host is needed, everything runs in a container through `dev.sh`:

```sh
./dev.sh build                  # dev image, once and after changes to requirements*.txt or the Dockerfile
./dev.sh test                   # the whole test suite
./dev.sh test tests/test_publish_feed.py -k rss    # arguments go to pytest
./dev.sh serve 8197             # app with reload on http://127.0.0.1:8197, no login
```

`./dev.sh serve` passes `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from your environment if they are set; without them
the AI features stay off. Data lives in `/tmp/podcast-studio-dev-<port>`.

## Tests

- `./dev.sh test` runs `pytest` with `PS_JOBS=0` and `PS_HOST_AUTH=none`. The suite takes a few minutes; the pipeline
  tests need `ffmpeg` and `deep-filter` (both are in the image) and download the small `tiny` Whisper model on the first
  run. Tests never use the large model.
- GitHub Actions runs the same suite on every pull request, plus a syntax check of all JavaScript under
  `app/static/js` and of the JSON dictionaries. A pull request should be green before review.
- **Browser tests** (Playwright with Chromium's fake camera and microphone) are not part of CI. They live in
  `tests/e2e/` (studio, `.mjs`) and `tests/e2e-manage/` (management pages, `.cjs`). Install Playwright somewhere
  (`npm install playwright && npx playwright install chromium`) and point `PLAYWRIGHT_PATH` at it, then start the app
  without a login and run a script:

  ```sh
  ./dev.sh serve-e2e 8196                                   # terminal 1
  PLAYWRIGHT_PATH=/path/to/node_modules/playwright \
    node tests/e2e/session.mjs                              # terminal 2 (BASE and DATA can be overridden)

  ./dev.sh serve 8195                                       # management pages: seed demo data first
  ./dev.sh run python scripts/seed_demo.py                  # see the header of the script for the data directory
  PLAYWRIGHT_PATH=/path/to/node_modules node tests/e2e-manage/smoke.cjs
  ```

  The header of each script says which server it expects. Please run the scripts that touch the area you changed.

## Language

The interface is available in German and English. **German is the source language**: write every user-visible text in
German in the code and wrap it in `t("Deutscher Text")` (JavaScript and templates: `{{ t("…") }}`); on the server
use `tr(lang, "Deutscher Text")` or an `ApiError` with the German message. The German text is the key. Add the English text to the dictionary of
that area under `app/static/i18n/` (`en.studio.json`, `en.manage.json`, `en.server.json`: German text → English text).
Placeholders such as `{n}` stay identical. `tests/test_i18n.py` and `tests/test_i18n_server.py` fail when a text is
missing. Details are in `docs/SPEC.md`, section "Sprache".

Code comments and the specification may stay German; documentation for users and contributors
(`README.md`, `docs/OPERATIONS.md`, this file) is English.

## Theme

Colors, fonts and shadows come from the theme in `app/static/design/`. Use its variables and `tr-*` classes instead of
hard-coded values, so a replaced theme still works. `docs/THEME.md` lists the interface.

## Commits and pull requests

- One topic per pull request, small and reviewable. Describe what changes for the user and why.
- Add or update tests with the change. Fix the cause, not the symptom, and do not weaken a test to make it pass.
- Commit messages: a short first line in the imperative or as a statement, then a paragraph with the reason. German or
  English is fine.
- Do not commit secrets, `.env` files, recordings or real show data. Examples use `example.org`.
- By contributing you agree that your contribution is licensed under the MIT license (see [LICENSE](LICENSE)).
