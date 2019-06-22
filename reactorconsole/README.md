# reactorconsole

Local logic for the "reactor console" for the odysseys.

Specs in Finnish at [this gdoc][gdoc]

[gdoc]: https://docs.google.com/document/d/1zdeoUQ3YgztciU4yB1AYPC0FBCUCMWxsuXX7z59Or_U/edit#

## Installation

  - Create virtualenv
  - Make sure you are within the odysseus-python checkout (since that is not properly packaged so we can't just pip install it)
  - `pip install --upgrade pip`
  - `pip install -r requirements.txt`

## Development

  - `pip install -r requirements_dev.txt`
  - Always work in a branch
  - Use `autopep8_and_friends.sh` before committing
  - Use PyTest `py.test -vvv` (see the `tests` folder for examples)
  - Commit early, commit often. You can always clean up the history later.

## Packaging (Linux)

Use `pyinstaller` to create "frozen" package that *should* run on any Linux
with equal or newer libc than the one you use to package it.

  - run `make package`

You should now have `reactorconsole-something.tar.gz` where something is the
current git revision.
