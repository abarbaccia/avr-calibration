"""Shared transducer profile library.

Profiles under ``transducers/`` are loaded at graph-construction time and
merged with any profiles the user declares inline in
``config.yaml → signal_graph.transducer_profiles``. A user-declared profile
with the same name as a built-in overrides it — so shipped profiles are
sensible defaults that the user can refine locally.

Adding a new profile:
  1. Drop a YAML file in ``transducers/<name>.yaml`` with the ``TransducerProfile``
     fields (see ``calibrate/graph.py``).
  2. Reference it by ``name`` from a ``Transducer.safety_profile_ref`` in
     ``config.yaml`` or elsewhere.

No code change required — the loader picks up new files automatically.
"""
