"""Drop-in compatibility shims.

``omegaconf_shim.install()`` makes ``import omegaconf`` resolve to hydra-fast's
implementation, so third-party code that registers resolvers or builds configs
against the ``omegaconf`` module lands in the fast registry without being
modified.
"""

from . import omegaconf_shim

__all__ = ["omegaconf_shim"]
