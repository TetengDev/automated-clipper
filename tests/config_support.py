"""Shared test config data: a valid `creators.yaml`.

Kept out of `conftest.py` so tests can import it by a unique module name; a bare
`from conftest import ...` resolves to whichever `conftest.py` pytest loaded last
(for example `tests/e2e/conftest.py`).
"""

VALID_YAML = """\
settings:
  min_duration_seconds: 300
  max_clips_per_source: 3
creators:
  - name: MrBeast
    region: global
    youtube:
      handle: "@MrBeast"
  - name: Kai Cenat
    region: global
    twitch:
      login: kaicenat
    youtube:
      handle: "@KaiCenat"
  - name: Raffy Tulfo in Action
    region: ph
    language: tl
    youtube:
      handle: "@RaffyTulfoInAction"
    overrides:
      min_duration_seconds: 600
"""
