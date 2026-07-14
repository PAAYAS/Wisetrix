"""WebLogic-to-Docker upgrade support.

Docker-to-Docker upgrades scan a normalized ``app_root/repos`` tree. A WebLogic
customer deployment has a different layout (``plugins/IMPLEMENTATION/...`` with
``$``-delimited category folders, environment overlays, and business-process
folders that redirect to different SYSTEM categories).

This package provides the WebLogic-specific *scan + routing* front end. Every
artifact it emits carries an explicit ``source_abs`` (the real source directory)
and ``system_rel`` (the normalized target-relative path), so the existing
3-way compare/merge pipeline consumes WebLogic artifacts unchanged.

Activated only when a project sets ``upgrade_mode: "weblogic"``. Docker-to-Docker
projects never import or run this code.
"""

from upgrade_lib.weblogic.scan import scan_weblogic_artifacts

__all__ = ["scan_weblogic_artifacts"]
