"""Directory-plugin shim for the packaged Talaria implementation.

Hermes's directory loader execs this file with ``__package__`` set, so the
relative import is the production path. The bare-module branch is for
pytest, which imports a rootdir ``__init__.py`` during collection (observed
under pytest 9.1) with ``__package__`` empty — there the absolute import
resolves the sibling ``talaria/`` package via the repository root on
``sys.path``. No pip distribution of this plugin exists (removed with
#351-K), so the absolute form cannot bind anything else.
"""

if __package__:
    from .talaria import register
else:
    from talaria import register

__all__ = ["register"]
