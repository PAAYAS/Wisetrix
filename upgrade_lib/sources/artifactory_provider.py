"""
ArtifactoryProvider — downloads and extracts JAR files from JFrog Artifactory.

Downloads gtm-install JARs from URLs like:
  https://sv4.art.e2open.com/ui/native/gtm-release-dev/com/amberroad/solutions/gtm-install/26.2/

Extracts to ~/.wisetrix/artifactory_cache/{version}/ and resolves
the SYSTEM path at: {extract_dir}/app_root/repos/SYSTEM

Authentication via config.yaml (basic auth or API key).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from upgrade_lib.sources.base_provider import SourceProvider, ResolvedSource


logger = logging.getLogger(__name__)

_CACHE_ROOT = Path.home() / ".wisetrix" / "artifactory_cache"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _persist_sidecar(sidecar: Path, system_path: Path) -> None:
    """Persist the resolved SYSTEM path so we never rglob this cache again."""
    try:
        sidecar.write_text(str(system_path), encoding="utf-8")
    except OSError:
        pass  # best-effort — don't fail resolve over a cache write


def _config_path() -> Path:
    """Find config file — supports both config.yaml and config.yml."""
    for name in ("config.yaml", "config.yml"):
        p = _PROJECT_ROOT / name
        if p.exists():
            return p
    return _PROJECT_ROOT / "config.yaml"


def _load_artifactory_credentials() -> dict:
    """Load Artifactory credentials from config.yaml / config.yml."""
    path = _config_path()
    if not path.exists():
        return {}
    if yaml is None:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return config.get("artifactory", {})
    except Exception:
        return {}


def _version_from_url(url: str) -> str:
    """Extract version string from Artifactory URL."""
    # Match patterns like /gtm-install/26.2/ or /gtm-install-26.2
    m = re.search(r"gtm-install[/-]([\d.]+)", url)
    if m:
        return m.group(1)
    # Fallback: use hash of URL
    return hashlib.sha256(url.encode()).hexdigest()[:8]


def _same_host(a: str, b: str) -> bool:
    """True if two URLs share the same host. Used to decide whether the
    Artifactory credentials apply to a given download."""
    try:
        return urlparse(a).hostname == urlparse(b).hostname
    except Exception:
        return False


def _ui_to_api_url(url: str) -> str:
    """Convert Artifactory UI URL to REST API / download URL.

    UI:  https://sv4.art.e2open.com/ui/native/gtm-release-dev/com/.../26.2/
    API: https://sv4.art.e2open.com/artifactory/gtm-release-dev/com/.../26.2/
    """
    return url.replace("/ui/native/", "/artifactory/")


def _find_jar_url(base_url: str, auth: tuple | None = None) -> str:
    """
    Given an Artifactory directory URL, find the actual JAR file URL.

    Strategy:
      1. Convert UI URL → API URL (ui/native → artifactory)
      2. Try direct JAR/ZIP candidates via HEAD on API URL
      3. Fall back to Artifactory Storage API to list children
      4. Fall back to HTML directory listing
    """
    if requests is None:
        raise ImportError("requests is required for Artifactory integration")

    base_url = base_url.rstrip("/")

    # Direct file link (e.g. a Nexus release jar, as used for the WebLogic
    # baseline): the URL already points at the artifact, not a directory to
    # list. Use it as-is — the download step surfaces a clear HTTP error if it
    # doesn't exist. No-op for Artifactory directory URLs (they don't end .jar).
    if base_url.lower().endswith((".jar", ".zip")):
        return base_url

    api_base = _ui_to_api_url(base_url)
    version = _version_from_url(base_url)

    # 1. Try direct JAR/ZIP download via API URL
    candidates = [
        f"{api_base}/gtm-install-{version}.jar",
        f"{api_base}/gtm-install-{version}.zip",
    ]
    for url in candidates:
        try:
            resp = requests.head(url, auth=auth, timeout=15, allow_redirects=True)
            if resp.status_code == 200:
                logger.info("Found artifact at %s", url)
                return url
        except requests.RequestException:
            continue

    # 2. Try Artifactory Storage API (JSON response with children)
    # /api/storage/repo-key/path → { children: [ { uri: "/file.jar", folder: false } ] }
    storage_path = api_base.replace("/artifactory/", "/artifactory/api/storage/", 1)
    try:
        resp = requests.get(storage_path, auth=auth, timeout=15, headers={"Accept": "application/json"})
        if resp.status_code == 200:
            try:
                data = resp.json()
                children = data.get("children", [])
                for child in children:
                    uri = child.get("uri", "")
                    if not child.get("folder", False) and (uri.endswith(".jar") or uri.endswith(".zip")):
                        download_url = f"{api_base}{uri}"
                        logger.info("Found artifact via Storage API: %s", download_url)
                        return download_url
            except (ValueError, KeyError):
                pass
    except requests.RequestException:
        pass

    # 3. Fall back to HTML directory listing
    try:
        resp = requests.get(api_base, auth=auth, timeout=15)
        if resp.status_code == 200:
            content = resp.text
            for ext in (".jar", ".zip"):
                m = re.search(rf'href="([^"]*{re.escape(ext)}[^"]*)"', content)
                if m:
                    href = m.group(1)
                    if href.startswith("http"):
                        return href
                    return f"{api_base.rstrip('/')}/{href.lstrip('/')}"
    except requests.RequestException:
        pass

    raise ValueError(
        f"Could not find JAR/ZIP file at {base_url}. "
        "Verify the Artifactory URL and ensure the artifact exists."
    )


class ArtifactoryProvider(SourceProvider):
    """Downloads and extracts JARs from JFrog Artifactory."""

    provider_type = "artifactory"

    def resolve(
        self,
        config: dict,
        progress_cb: Callable[[str, dict], None] | None = None,
    ) -> ResolvedSource:
        if requests is None:
            raise ImportError(
                "requests is required for Artifactory integration. "
                "Install it with: pip install requests"
            )

        errors = self.validate(config)
        if errors:
            raise ValueError(f"Invalid artifactory config: {'; '.join(errors)}")

        # Load credentials
        creds = _load_artifactory_credentials()
        auth = None
        if creds.get("username") and creds.get("password"):
            auth = (creds["username"], creds["password"])

        # Resolve target
        target_url = config.get("artifactory_url", "")
        target_version = config.get("target_version", _version_from_url(target_url))
        target_path = self._download_and_extract(
            target_url, target_version, auth, progress_cb=progress_cb
        )

        # Resolve baseline (optional)
        baseline_path = None
        baseline_url = config.get("baseline_url") or config.get("baseline_artifactory_url", "")
        if baseline_url:
            baseline_version = config.get("baseline_version", _version_from_url(baseline_url))
            # The baseline may live on a different server than the target (e.g. a
            # public Nexus repo for WebLogic baselines) that rejects the
            # Artifactory credentials with a 401. Only send credentials when the
            # baseline is on the same host the credentials belong to.
            baseline_auth = auth if _same_host(baseline_url, target_url) else None
            baseline_path = self._download_and_extract(
                baseline_url, baseline_version, baseline_auth, progress_cb=progress_cb
            )

        return ResolvedSource(
            source_root=Path(config.get("source_root", "")),  # resolved separately
            target_system=target_path,
            baseline_system=baseline_path,
            metadata={
                "provider": "artifactory",
                "target_url": target_url,
                "target_version": target_version,
                "baseline_url": baseline_url or None,
                "baseline_version": config.get("baseline_version"),
            },
        )

    def validate(self, config: dict) -> list[str]:
        errors = []
        if not config.get("artifactory_url"):
            errors.append("artifactory_url is required")
        if requests is None:
            errors.append("requests package is not installed")
        return errors

    def _download_and_extract(
        self,
        url: str,
        version: str,
        auth: tuple | None,
        progress_cb: Callable[[str, dict], None] | None = None,
    ) -> Path:
        """Download JAR and extract to cache. Returns SYSTEM path.

        progress_cb(phase, data) is called at key points so callers can
        surface progress to a UI or log stream without polling.
        """
        def _emit(phase: str, **data: object) -> None:
            if progress_cb:
                try:
                    progress_cb(phase, {"version": version, **data})
                except Exception:
                    pass  # never let a progress callback crash the download

        cache_dir = _CACHE_ROOT / version
        system_path = self._find_system_path(cache_dir)

        if system_path:
            logger.info("Using cached extraction for version %s", version)
            _emit("cached", msg=f"v{version} already extracted locally")
            return system_path

        # Download
        jar_url = _find_jar_url(url, auth)
        logger.info("Downloading %s", jar_url)

        cache_dir.mkdir(parents=True, exist_ok=True)
        jar_path = cache_dir / f"gtm-install-{version}.jar"

        resp = requests.get(jar_url, auth=auth, stream=True, timeout=300)
        resp.raise_for_status()

        total_bytes = int(resp.headers.get("content-length", 0))
        total_mb = round(total_bytes / 1024 / 1024, 1) if total_bytes else None
        _emit(
            "download_start",
            total_mb=total_mb,
            msg=f"Downloading v{version} JAR"
                + (f" ({total_mb} MB)" if total_mb else ""),
        )

        downloaded = 0
        last_pct = -1
        last_emit_time = time.monotonic()

        with open(jar_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65_536):  # 64 KB chunks
                f.write(chunk)
                downloaded += len(chunk)

                # Emit at most once per 2 seconds or every 5% — whichever comes first
                now = time.monotonic()
                pct = int(downloaded / total_bytes * 100) if total_bytes else 0
                if (now - last_emit_time >= 2.0) or (pct >= last_pct + 5):
                    dl_mb = round(downloaded / 1024 / 1024, 1)
                    _emit(
                        "download_progress",
                        downloaded_mb=dl_mb,
                        total_mb=total_mb,
                        pct=pct,
                        msg=f"Downloading v{version}… "
                            + (f"{dl_mb} / {total_mb} MB ({pct}%)" if total_mb
                               else f"{dl_mb} MB downloaded"),
                    )
                    last_pct = pct
                    last_emit_time = now

        dl_mb = round(downloaded / 1024 / 1024, 1)
        logger.info("Downloaded %.1f MB for version %s", dl_mb, version)
        _emit("download_done", downloaded_mb=dl_mb, msg=f"Download complete ({dl_mb} MB)")

        # Extract (JAR is a ZIP).
        # We only need the SYSTEM artifacts tree — skip compiled Java classes,
        # documentation, and everything else in the JAR.  Partial extraction
        # cuts this step from 1-3 minutes down to 15-30 seconds for large JARs.
        logger.info("Extracting %s (partial — SYSTEM path only)", jar_path)
        extract_dir = cache_dir / f"gtm-install-{version}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(jar_path, "r") as zf:
            all_members = zf.namelist()
            # Collect every entry that lives under app_root/repos/SYSTEM
            system_members = [
                m for m in all_members
                if "app_root/repos/SYSTEM" in m.replace("\\", "/")
            ]
            if system_members:
                logger.info(
                    "Partial extraction: %d / %d files (under app_root/repos/SYSTEM)",
                    len(system_members),
                    len(all_members),
                )
                _emit(
                    "extract_start",
                    files=len(system_members),
                    msg=f"Extracting v{version} ({len(system_members):,} files)…",
                )
                for member in system_members:
                    zf.extract(member, extract_dir)
            else:
                # Unexpected JAR layout — fall back to full extraction so we
                # never silently produce an empty SYSTEM directory.
                logger.warning(
                    "app_root/repos/SYSTEM not found in ZIP entries — "
                    "falling back to full extraction (%d files)",
                    len(all_members),
                )
                _emit(
                    "extract_start",
                    files=len(all_members),
                    msg=f"Extracting v{version} (full — {len(all_members):,} files)…",
                )
                zf.extractall(extract_dir)

        _emit("extract_done", msg=f"v{version} extracted successfully")

        # Clean up JAR to save disk space
        jar_path.unlink(missing_ok=True)

        system_path = self._find_system_path(cache_dir)
        if not system_path:
            raise ValueError(
                f"Could not find app_root/repos/SYSTEM in extracted content at {cache_dir}. "
                "The JAR structure may differ from expected."
            )
        return system_path

    @staticmethod
    def _find_system_path(cache_dir: Path) -> Path | None:
        """Find the SYSTEM repos path within extracted content.

        Strategy (fastest first):
          1. Read sidecar file `.wisetrix_system_path` if it was written by a
             previous resolve — eliminates the rglob entirely.
          2. Try well-known paths directly (no traversal).
          3. Fall back to rglob (expensive — only on first resolve).
        On success via (2) or (3), persist the path to the sidecar so the next
        call hits (1).
        """
        if not cache_dir.exists():
            return None

        sidecar = cache_dir / ".wisetrix_system_path"
        if sidecar.exists():
            try:
                cached = Path(sidecar.read_text(encoding="utf-8").strip())
                if cached.is_dir():
                    return cached
            except Exception:
                pass  # corrupt sidecar — fall through and rebuild

        # Well-known patterns based on how _download_and_extract lays out the JAR.
        # Most JARs have either a flat app_root or a single top-level dir.
        for candidate in cache_dir.iterdir() if cache_dir.is_dir() else []:
            if not candidate.is_dir():
                continue
            for sub in ("app_root/repos/SYSTEM", "SYSTEM"):
                p = candidate / sub
                if p.is_dir():
                    _persist_sidecar(sidecar, p)
                    return p
        direct = cache_dir / "app_root" / "repos" / "SYSTEM"
        if direct.is_dir():
            _persist_sidecar(sidecar, direct)
            return direct

        # Last resort: the slow path.
        for p in cache_dir.rglob("app_root/repos/SYSTEM"):
            if p.is_dir():
                _persist_sidecar(sidecar, p)
                return p
        return None

    def test_connection(self, url: str) -> dict:
        """Test connection to Artifactory URL. Returns status dict."""
        creds = _load_artifactory_credentials()
        auth = None
        if creds.get("username") and creds.get("password"):
            auth = (creds["username"], creds["password"])

        # Convert UI URL to API URL for testing
        api_url = _ui_to_api_url(url)

        try:
            # Try Storage API first (most reliable)
            storage_url = api_url.replace("/artifactory/", "/artifactory/api/storage/", 1)
            resp = requests.get(
                storage_url, auth=auth, timeout=15,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    children = data.get("children", [])
                    jars = [c["uri"] for c in children if c.get("uri", "").endswith(".jar")]
                    return {
                        "success": True,
                        "status_code": 200,
                        "message": f"Connected. Found {len(children)} items"
                                   + (f" including {jars[0]}" if jars else ""),
                    }
                except (ValueError, KeyError):
                    return {"success": True, "status_code": 200, "message": "Connected (non-JSON response)"}

            # Fall back to HEAD on API URL
            resp = requests.head(api_url, auth=auth, timeout=15, allow_redirects=True)
            return {
                "success": resp.status_code < 400,
                "status_code": resp.status_code,
                "message": "Connection successful" if resp.status_code < 400
                           else f"HTTP {resp.status_code}",
            }
        except requests.RequestException as e:
            return {"success": False, "status_code": None, "message": str(e)}
