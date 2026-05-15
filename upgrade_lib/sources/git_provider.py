"""
GitProvider — clones/pulls Bitbucket git repos for source resolution.

Caches clones in ~/.yantrix/git_cache/{repo_hash}/ to avoid
re-downloading on every run. Supports SSH and HTTPS URLs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import git as gitpython
except ImportError:
    gitpython = None  # type: ignore[assignment]

from upgrade_lib.sources.base_provider import SourceProvider, ResolvedSource


logger = logging.getLogger(__name__)

# Default subpath from repo root to the artifact repos directory.
DEFAULT_SOURCE_SUBPATH = "client_delivery/src/main/resources/app_root/repos"

# Cache directory for cloned repos.
_CACHE_ROOT = Path.home() / ".yantrix" / "git_cache"

# In-process locks keyed by repo hash — serialize fetch/checkout/pull within
# a single uvicorn worker. Cross-process serialization is handled by the
# file-lock below.
_PROC_LOCKS: dict[str, threading.Lock] = {}
_PROC_LOCKS_MUTEX = threading.Lock()

# How long to wait for another process to release the cache lock.
_LOCK_TIMEOUT_SECONDS = 120
# How old a `.git/index.lock` must be before we treat it as orphaned and remove it.
_STALE_INDEX_LOCK_SECONDS = 30


def _repo_hash(url: str) -> str:
    """Stable short hash for a git URL, used as cache key."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _get_proc_lock(key: str) -> threading.Lock:
    with _PROC_LOCKS_MUTEX:
        lock = _PROC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROC_LOCKS[key] = lock
        return lock


@contextmanager
def _cache_lock(clone_dir: Path):
    """Cross-process + cross-thread lock for one cached repo.

    Uses a sentinel file next to the cache dir, acquired via O_CREAT|O_EXCL so
    the kernel guarantees mutual exclusion across processes. Threads within
    the same process serialize on `_PROC_LOCKS` first to avoid spinning.
    """
    repo_key = clone_dir.name
    lockfile = clone_dir.parent / f"{repo_key}.lock"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    proc_lock = _get_proc_lock(repo_key)

    proc_lock.acquire()
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        fd = None
        while True:
            try:
                fd = os.open(
                    str(lockfile),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                break
            except FileExistsError:
                # Reap stale lockfiles (process died holding it).
                try:
                    age = time.time() - lockfile.stat().st_mtime
                    if age > _LOCK_TIMEOUT_SECONDS:
                        logger.warning(
                            "Removing stale cache lock (%.0fs old): %s",
                            age,
                            lockfile,
                        )
                        lockfile.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Timed out waiting for git cache lock: {lockfile}"
                    )
                time.sleep(0.25)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)

        # Also reap an orphaned `.git/index.lock` if a previous worker was
        # killed mid-checkout. We hold the cache lock so this is safe.
        _remove_stale_index_lock(clone_dir)

        try:
            yield
        finally:
            try:
                lockfile.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        proc_lock.release()


def _remove_stale_index_lock(clone_dir: Path) -> None:
    idx_lock = clone_dir / ".git" / "index.lock"
    try:
        age = time.time() - idx_lock.stat().st_mtime
    except FileNotFoundError:
        return
    except OSError:
        return
    if age >= _STALE_INDEX_LOCK_SECONDS:
        logger.warning(
            "Removing stale .git/index.lock (%.0fs old): %s", age, idx_lock
        )
        try:
            idx_lock.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove %s: %s", idx_lock, e)


class GitProvider(SourceProvider):
    """Resolves source artifacts from a cloned Bitbucket git repo."""

    provider_type = "git"

    def resolve(self, config: dict, *, skip_pull: bool = False) -> ResolvedSource:
        if gitpython is None:
            raise ImportError(
                "gitpython is required for Git integration. "
                "Install it with: pip install gitpython"
            )

        errors = self.validate(config)
        if errors:
            raise ValueError(f"Invalid git config: {'; '.join(errors)}")

        git_url = config["git_url"]
        branch = config.get("git_branch", "main")
        subpath = config.get("source_subpath", DEFAULT_SOURCE_SUBPATH)

        clone_dir = _CACHE_ROOT / _repo_hash(git_url)
        clone_dir.parent.mkdir(parents=True, exist_ok=True)

        # Fast path: clone exists and caller doesn't need a fresh pull.
        # Skips network entirely — used by the merge endpoint, which works
        # off the state that the most recent scan already validated.
        if skip_pull and clone_dir.exists() and (clone_dir / ".git").exists():
            try:
                repo = gitpython.Repo(clone_dir)
                commit_hash = repo.head.commit.hexsha
                source_root = clone_dir / subpath
                if source_root.exists():
                    return ResolvedSource(
                        source_root=source_root,
                        target_system=self._resolve_target(config),
                        baseline_system=self._resolve_baseline(config),
                        metadata={
                            "provider": "git",
                            "git_url": git_url,
                            "branch": branch,
                            "commit": commit_hash,
                            "clone_dir": str(clone_dir),
                            "skipped_pull": True,
                        },
                    )
            except Exception as e:
                logger.warning(
                    "skip_pull fast-path failed (%s); falling back to full resolve",
                    e,
                )

        with _cache_lock(clone_dir):
            # Clone or pull (serialized across threads/processes)
            if clone_dir.exists() and (clone_dir / ".git").exists():
                logger.info("Updating %s (branch: %s)", git_url, branch)
                repo = gitpython.Repo(clone_dir)
                # Fetch only the target branch (single network call) and
                # fast-forward via reset --hard. This avoids the
                # `fetch --all --prune` + `pull` combo, which makes two
                # network round-trips over every ref in the repo.
                repo.git.fetch("origin", branch, "--no-tags")
                # Make sure local branch exists and points at FETCH_HEAD.
                try:
                    repo.git.checkout(branch)
                except Exception:
                    # Branch not present locally yet — create tracking branch.
                    repo.git.checkout("-B", branch, f"origin/{branch}")
                repo.git.reset("--hard", f"origin/{branch}")
            else:
                logger.info("Cloning %s (branch: %s)", git_url, branch)
                # `--single-branch --no-tags` skips fetching unused branches/tags.
                repo = gitpython.Repo.clone_from(
                    git_url,
                    str(clone_dir),
                    branch=branch,
                    single_branch=True,
                    no_tags=True,
                )

            commit_hash = repo.head.commit.hexsha
        source_root = clone_dir / subpath

        if not source_root.exists():
            raise ValueError(
                f"Source subpath not found after clone: {source_root}\n"
                f"Check 'source_subpath' in your project config."
            )

        # Build ResolvedSource — target/baseline are resolved separately
        # (typically via ArtifactoryProvider or LocalProvider)
        target_system = self._resolve_target(config)
        baseline_system = self._resolve_baseline(config)

        return ResolvedSource(
            source_root=source_root,
            target_system=target_system,
            baseline_system=baseline_system,
            metadata={
                "provider": "git",
                "git_url": git_url,
                "branch": branch,
                "commit": commit_hash,
                "clone_dir": str(clone_dir),
            },
        )

    def validate(self, config: dict) -> list[str]:
        errors = []
        if not config.get("git_url"):
            errors.append("git_url is required")
        if gitpython is None:
            errors.append("gitpython package is not installed")
        return errors

    def list_branches(self, git_url: str) -> list[str]:
        """List remote branches for a git URL (for UI branch selector)."""
        if gitpython is None:
            raise ImportError("gitpython is required")

        clone_dir = _CACHE_ROOT / _repo_hash(git_url)

        with _cache_lock(clone_dir):
            if clone_dir.exists() and (clone_dir / ".git").exists():
                repo = gitpython.Repo(clone_dir)
                repo.git.fetch("--all", "--prune")
            else:
                repo = gitpython.Repo.clone_from(git_url, str(clone_dir))

        branches = []
        for ref in repo.references:
            name = str(ref)
            if name.startswith("origin/") and not name.endswith("/HEAD"):
                branches.append(name.replace("origin/", "", 1))
        return sorted(set(branches))

    @staticmethod
    def _resolve_target(config: dict) -> Path:
        """Resolve target SYSTEM path from config."""
        # If target is local path
        if config.get("target_system"):
            return Path(config["target_system"])
        # If target is artifactory (resolved separately)
        if config.get("target_type") == "artifactory":
            # ArtifactoryProvider will handle this
            return Path(config.get("target_local_path", ""))
        raise ValueError("No target_system or target_type specified")

    @staticmethod
    def _resolve_baseline(config: dict) -> Path | None:
        """Resolve baseline SYSTEM path from config."""
        if config.get("baseline_system"):
            p = Path(config["baseline_system"])
            return p if str(p) else None
        if config.get("baseline_local_path"):
            return Path(config["baseline_local_path"])
        return None

    def extract_jira_keys_for_paths(
        self,
        git_url: str,
        branch: str = "main",
        artifact_paths: list[str] | None = None,
    ) -> dict[str, set[str]]:
        """
        Extract JIRA ticket keys from git commit messages that touched artifact paths.

        Scans commit messages for patterns like PROJ-123, ALDIBR1-456, etc.
        If artifact_paths is provided, only returns keys for commits touching those paths.
        Otherwise scans all commits on the branch.

        Returns dict mapping artifact_rel_path → set of JIRA keys found in commits.
        """
        if gitpython is None:
            raise ImportError("gitpython is required")

        clone_dir = _CACHE_ROOT / _repo_hash(git_url)
        if not clone_dir.exists() or not (clone_dir / ".git").exists():
            raise ValueError(f"Repo not cloned yet. Run resolve() first.")

        repo = gitpython.Repo(clone_dir)
        jira_key_re = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

        result: dict[str, set[str]] = {}

        # Walk commits on the branch (limit to last 500 for performance)
        try:
            commits = list(repo.iter_commits(branch, max_count=500))
        except Exception:
            commits = list(repo.iter_commits(max_count=500))

        for commit in commits:
            keys = jira_key_re.findall(commit.message)
            if not keys:
                continue

            # Get files changed in this commit
            try:
                if commit.parents:
                    diffs = commit.diff(commit.parents[0])
                else:
                    diffs = commit.diff(gitpython.NULL_TREE)
                changed_paths = []
                for d in diffs:
                    if d.a_path:
                        changed_paths.append(d.a_path)
                    if d.b_path and d.b_path != d.a_path:
                        changed_paths.append(d.b_path)
            except Exception:
                continue

            for path in changed_paths:
                # Match against artifact paths if provided
                if artifact_paths:
                    matched_artifact = None
                    for art_path in artifact_paths:
                        if art_path in path or path.endswith(art_path):
                            matched_artifact = art_path
                            break
                    if matched_artifact:
                        result.setdefault(matched_artifact, set()).update(keys)
                else:
                    result.setdefault(path, set()).update(keys)

        return result
