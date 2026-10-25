"""The parts of ``config.py`` behind its facade: the model registry
(``models``), the system prompt's rendering (``prompts``) and the value
parsers with the key registry (``readers``). At import a module loads only
the standard library, ``psycopg`` and the vocabulary leaves (``auth.roles``,
``core.layout``, ``core.placement``), so importing one never runs ``config``.
"""
