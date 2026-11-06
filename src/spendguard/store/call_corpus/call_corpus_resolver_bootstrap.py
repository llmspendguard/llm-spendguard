"""Import the lazy callio home so its accessor resolvers register."""


def wire_call_corpus_accessor_resolvers() -> None:
    from . import callio  # noqa: F401
