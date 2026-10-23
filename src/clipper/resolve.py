"""`clipper resolve` (CLP-22): creator handles -> stable platform IDs in creators.yaml.

Every handle is looked up on each run, so a handle edited to point at another channel
gets that channel's ID. The file is rewritten only when an ID actually changes, so a
re-run with nothing new leaves it byte-identical. The rewrite is a ruamel.yaml round trip
(comments, order and quoting kept) that must load as a valid config before it atomically
replaces the file.
"""

from __future__ import annotations

import copy
import io
import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from clipper.config import ConfigError, Creator, load_config
from clipper.sources.twitch import TwitchError, app_token, user_ids
from clipper.sources.youtube import HandleNotFound, MetadataProvider, SourceError
from clipper.urls import Platform

# platform -> (human handle key, resolved id key) in creators.yaml
_KEYS: dict[str, tuple[str, str]] = {
    "youtube": ("handle", "channel_id"),
    "twitch": ("login", "user_id"),
}


@dataclass(frozen=True)
class Resolved:
    creator: str
    platform: Platform
    handle: str
    old: str | None
    new: str | None = None
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.new is not None and self.new != self.old


def resolve_youtube(creators: Iterable[Creator], provider: MetadataProvider) -> list[Resolved]:
    """Look up every YouTube handle; a failure is reported for that creator only."""
    results = []
    for creator in creators:
        if creator.youtube is None:
            continue
        handle, old = creator.youtube.handle, creator.youtube.channel_id
        try:
            new = provider.channel_id(handle)
        except (HandleNotFound, SourceError) as exc:
            results.append(Resolved(creator.name, "youtube", handle, old, error=str(exc)))
        else:
            results.append(Resolved(creator.name, "youtube", handle, old, new))
    return results


def resolve_twitch(
    creators: Iterable[Creator], http: httpx.Client, client_id: str, client_secret: str
) -> list[Resolved]:
    """Look up every Twitch login with one app token (Helix /users, 100 logins per call)."""
    wanted = [(c.name, c.twitch.login, c.twitch.user_id) for c in creators if c.twitch]
    if not wanted:
        return []
    try:
        token = app_token(http, client_id, client_secret)
        found = user_ids(http, token, client_id, [login for _, login, _ in wanted])
    except TwitchError as exc:
        return [Resolved(n, "twitch", login, old, error=str(exc)) for n, login, old in wanted]
    return [
        Resolved(name, "twitch", login, old, found[login])
        if login in found
        else Resolved(name, "twitch", login, old, error=f"no Twitch user has the login {login}")
        for name, login, old in wanted
    ]


def _yaml() -> YAML:
    yaml = YAML()  # round-trip mode
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096  # never re-wrap long lines
    return yaml


def _insert_after(mapping: CommentedMap, after: str, key: str, value: Any) -> None:
    """Insert `key` right after `after`. ruamel attaches the blank lines and comments that
    follow a mapping to its last key; when `after` was last they move to the new key
    (else they would end up above it), while an end-of-line comment stays on its line."""
    keys = list(mapping)
    mapping.insert(keys.index(after) + 1, key, value)
    slot = mapping.ca.items.get(after)
    if keys[-1] != after or not slot or len(slot) < 3 or slot[2] is None:
        return
    token = slot[2]
    eol, newline, following = token.value.partition("\n")
    if not following:
        return
    moved = copy.copy(token)
    moved.value = "\n" + following
    mapping.ca.items[key] = [None, None, moved, None]
    if eol.strip():
        token.value = eol + newline
    else:
        slot[2] = None


def write_ids(path: Path, results: Sequence[Resolved]) -> int:
    """Write changed IDs into `path`; returns how many. Nothing changed: file untouched.

    Raises ValueError (file untouched) if the result would not be a valid config."""
    changes = [r for r in results if r.changed]
    if not changes:
        return 0
    yaml = _yaml()
    doc = yaml.load(path.read_text(encoding="utf-8"))
    nodes = {str(node.get("name")).casefold(): node for node in doc["creators"]}
    for result in changes:
        node = nodes[result.creator.casefold()][result.platform]
        handle_key, id_key = _KEYS[result.platform]
        # A bare all-digit user_id would load as an int; quote it like the handles are.
        value = DoubleQuotedScalarString(result.new) if result.platform == "twitch" else result.new
        if id_key in node:
            node[id_key] = value
        else:
            _insert_after(node, handle_key, id_key, value)
    out = io.StringIO()
    yaml.dump(doc, out)
    # Same directory (atomic replace); a fresh random name created with O_EXCL, so a file
    # or symlink planted at a predictable name is never written through.
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        tmp = Path(handle.name)
    try:
        tmp.write_text(out.getvalue(), encoding="utf-8")
        shutil.copymode(path, tmp)
        try:
            load_config(tmp)
        except ConfigError as exc:
            raise ValueError(f"not writing {path}: the result would be invalid: {exc}") from None
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return len(changes)
