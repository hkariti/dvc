import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Optional

from dvc.repo import locked
from dvc.repo.scm_context import scm_context
from dvc.scm import Git
from dvc.utils.fs import remove

from .exceptions import (
    ApplyConflictError,
    BaselineMismatchError,
    InvalidExpRevError,
)
from .executor.base import BaseExecutor
from .refs import EXEC_APPLY

if TYPE_CHECKING:
    from dvc.repo import Repo
    from dvc.repo.experiments import Experiments

logger = logging.getLogger(__name__)


@locked
@scm_context
def apply(repo: "Repo", rev: str, force: bool = True, **kwargs):  # noqa: C901
    from scmrepo.exceptions import SCMError as _SCMError

    from dvc.repo.checkout import checkout as dvc_checkout
    from dvc.scm import GitMergeError, RevError, resolve_rev

    exps: "Experiments" = repo.experiments

    is_stash: bool = False

    assert isinstance(repo.scm, Git)
    try:
        exp_rev = resolve_rev(repo.scm, rev)
        exps.check_baseline(exp_rev)
    except RevError as exc:
        (
            exp_ref_info,
            queue_entry,
        ) = exps.celery_queue.get_ref_and_entry_by_names(rev)[rev]
        baseline_sha = repo.scm.get_rev()
        if exp_ref_info:
            if baseline_sha != exp_ref_info.baseline_sha:
                raise InvalidExpRevError(rev)  # noqa: B904
            exp_rev = repo.scm.get_ref(str(exp_ref_info))
        elif queue_entry:
            if queue_entry.baseline_rev != baseline_sha:
                raise InvalidExpRevError(rev)
            exp_rev = queue_entry.stash_rev
            is_stash = True
        else:
            raise InvalidExpRevError(rev) from exc
    except BaselineMismatchError as exc:
        raise InvalidExpRevError(rev) from exc

    # NOTE: we don't use scmrepo's stash_workspace() here since we need
    # finer control over the merge behavior when we unstash everything
    with _apply_workspace(repo, rev, force):
        try:
            repo.scm.merge(exp_rev, commit=False, squash=True)
        except _SCMError as exc:
            raise GitMergeError(str(exc), scm=repo.scm)  # noqa: B904

    repo.scm.reset()

    if is_stash:
        assert repo.tmp_dir is not None
        args_path = os.path.join(repo.tmp_dir, BaseExecutor.PACKED_ARGS_FILE)
        if os.path.exists(args_path):
            remove(args_path)

    dvc_checkout(repo, **kwargs)

    repo.scm.set_ref(EXEC_APPLY, exp_rev)
    logger.info(
        "Changes for experiment '%s' have been applied to your current "
        "workspace.",
        rev,
    )


@contextmanager
def _apply_workspace(repo: "Repo", rev: str, force: bool):
    from scmrepo.exceptions import MergeConflictError
    from scmrepo.exceptions import SCMError as _SCMError

    assert isinstance(repo.scm, Git)
    if repo.scm.is_dirty(untracked_files=True):
        logger.debug("Stashing workspace")
        stash_rev: Optional[str] = repo.scm.stash.push(include_untracked=True)
    else:
        stash_rev = None
    try:
        yield
    except Exception:  # pylint: disable=broad-except
        if stash_rev:
            _clean_and_pop(repo.scm)
        raise
    if not stash_rev:
        return

    try:
        repo.scm.reset()
        repo.scm.stash.apply(stash_rev, skip_conflicts=force)
        repo.scm.stash.drop()
    except (MergeConflictError, _SCMError) as exc:
        _clean_and_pop(repo.scm)
        raise ApplyConflictError(rev) from exc
    except Exception:  # pylint: disable=broad-except
        _clean_and_pop(repo.scm)
        raise


def _clean_and_pop(scm: "Git"):
    """Revert any changes and pop the last stash entry."""
    scm.reset(hard=True)
    if scm.is_dirty(untracked_files=True):
        # drop any changes to untracked files before popping stash
        scm.stash.push(include_untracked=True)
        scm.stash.drop()
    scm.stash.pop()
