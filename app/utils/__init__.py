"""Utility modules: logging, formatting, validators, cache.

The implementations live in the submodules. A parallel consolidation had put
second copies of all four in this __init__.py, which meant every
`from app.utils.logging import ...` also executed `import structlog` here -
a hidden dependency on a package nothing in the bot path uses.
"""
