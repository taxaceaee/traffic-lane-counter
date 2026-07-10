"""Compatibility entrypoint for local tooling.

The canonical API app now lives at ``tf_api.main`` so Docker and local runs
share the same route set and security behavior.
"""

from tf_api.main import app, create_app, get_api_key
