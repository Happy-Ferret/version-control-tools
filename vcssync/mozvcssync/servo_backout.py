"""
`servo-backout-pr` listens for backouts pushed to the `integration-autoland`
repository and generates github pull requests against the `servo` repository.

It requires a fork of `servo/servo` in a github service account (currently
`moz-servo-sync`) where all the pull requests will be created.

When any change is pushed to integration-autoland, `cli.backout_servo_pr_cli`
is invoked which executes `backout_servo_pr`.

`backout_servo_pr` scans for backout commits, starting with the last commit it
processed.  Commit SHAs are extracted from the commit description, the original
servo PR is extracted from the backed out revision's commit description, and a
pull request is created.

In order to ensure integration-autoland's servo directory servo and
github/servo/servo continue to stay in sync, homu (servo's landing bot) will
merge this PR before any other requests in its queue, and `servo-vcs-sync`'s
overlay service is configured to ignore commits generated by this service.
"""

from __future__ import absolute_import, unicode_literals

import logging
import os
import re
import subprocess
from collections import namedtuple

import hglib
from hglib.util import cmdbuilder
from mozautomation import commitparser

from .github_pr import GitHubPR
from .util import (clean_hg_repo, apply_changes_from_list)

logger = logging.getLogger(__name__)

SERVO_MERGE_RE = re.compile(
    r'^servo: Merge #([0-9]+)'
)

LightWeightCommit = namedtuple('LightWeightCommit', ['node', 'desc', 'author'])


def _find_backout_commits(hg_repo, revset):
    commits = []
    for commit in hg_repo.log('%s - merge()' % revset,
                              files=['path:servo/'], removed=True):
        if commitparser.parse_backouts(commit.desc, strict=True):
            commits.append(LightWeightCommit(commit.node,
                                             commit.desc.decode('utf-8'),
                                             commit.author.decode('utf-8')))
    # Return oldest first, so they will be applied in the correct order.
    commits.reverse()
    return commits


def _find_backed_out_urls(hg_repo, github_url, commit_desc, commit_node):
    backed_out_urls = []
    revs, bugs = commitparser.parse_backouts(commit_desc, strict=True)

    for backed_out_rev in revs:
        backed_out_desc = hg_repo.log(backed_out_rev)[0].desc
        backed_out_desc = backed_out_desc.splitlines()[0]

        m = SERVO_MERGE_RE.match(backed_out_desc)
        if m:
            backed_out_url = '%s/pull/%s' % (github_url, m.group(1))
            backed_out_urls.append(backed_out_url)
            logger.info('%s backing out %s: %s'
                        % (commit_node[:12], backed_out_rev[:12],
                           backed_out_url))

        else:
            logger.warning('failed to find merge id in #%s: %s'
                           % (backed_out_rev, backed_out_desc))

    return backed_out_urls


def _get_touched_files(hg_repo, commit_node):
    # Build list of files touched by this commit.

    def strip_servo(filename):
        assert filename.startswith('servo/')
        return filename[len('servo/'):]

    # Grab a list of files under the servo/ directory touched by this
    # change.
    args = cmdbuilder('log', template='{join(files,"\n")}',
                      r=commit_node, I='path:servo/')
    return map(strip_servo, hg_repo.rawcommand(args).split('\n'))


def _build_commit_desc(commit_desc, backed_out_urls):
    # Build commit description and PR body.
    desc = commitparser.strip_commit_metadata(commit_desc).strip()
    if backed_out_urls:
        desc += '\n\n'
        for url in backed_out_urls:
            desc += 'Backs out %s' % url
    return desc


def _create_pr_from_backout(integration_repo_path, hg_repo, github_pr,
                            backout_commits, pull_request_author):
    for commit in backout_commits:
        logger.info('processing backout %s: %s'
                    % (commit.node[:12], commit.desc.splitlines()[0]))

        backed_out_urls = _find_backed_out_urls(
            hg_repo, github_pr.upstream_repo().html_url,
            commit.desc, commit.node)

        touched_files = _get_touched_files(hg_repo, commit.node)

        # Set up commit and PR metadata.

        branch_name = 'gecko-backout'

        if pull_request_author:
            author = pull_request_author
        else:
            author = commit.author

        desc = _build_commit_desc(commit.desc, backed_out_urls)

        # Remove detritus.
        clean_hg_repo(logger, integration_repo_path)

        # Checkout the backout commit to ensure we're copying the
        # correct file contents.
        hg_repo.update(rev=commit.node)

        # If there's a PR in flight, this commit should be appended to it.
        open_pr = github_pr.pr_from_branch(branch_name, state='open')
        if open_pr:
            logger.info('updating existing pr: %s' % open_pr.html_url)

        # Apply changes by copying files; this sidesteps any diff
        # issues.
        def apply_patch(_):
            apply_changes_from_list(logger, '%s/servo' % integration_repo_path,
                                    github_pr.repo_path, touched_files)

        # Create/update the pull request.
        pr = github_pr.create_pr_from_patch(
            branch_name=branch_name,
            reset_branch=open_pr is None,
            description=desc,
            author=author,
            pr_body=desc,
            pr_title_multiple='Multiple gecko backouts',
            patch_callback=apply_patch)

        if pr:
            # Tell homu this has been approved, and to abort any in-flight
            # work against the PR if we're updating it.
            # Unfortunately github3.py v0.9.6 doesn't implement create_comment.
            # It's implemented in github3.py v1 however that isn't stable yet.
            pr._post(pr.comments_url,
                     {'body': '@bors-servo r+ force p=9001 treeclosed=9000\n'})


def backout_servo_pr(integration_repo_url, integration_repo_path,
                     github_repo_name, github_repo_path,
                     pull_request_author=None,
                     tracking_s3_upload_url=None,
                     starting_revision=None):
    github_token = os.getenv('GITHUB_TOKEN')
    if github_token is None:
        raise Exception('GITHUB_TOKEN environmental var is not defined')

    if len(github_repo_name.split('/')) != 2:
        raise Exception('"%s" is not in the form "user/repo"'
                        % github_repo_name)

    # Initialise integration repo.
    if not os.path.exists(integration_repo_path):
        logger.warn('%s does not exist; cloning %s' % (integration_repo_path,
                                                       integration_repo_url))
        subprocess.check_call([hglib.HGPATH, b'clone', b'--noupdate',
                               b'--pull', integration_repo_url,
                              integration_repo_path])

    # Read last processed revision.
    tracking_file = os.path.join(integration_repo_path, b'.hg', b'backout-rev')
    if starting_revision:
        # When a revision is provided process that revision and its
        # descendants.
        revset = 'descendants({rev})'.format(rev=starting_revision)
    else:
        # We never want to walk all commits.
        if not os.path.exists(tracking_file):
            raise Exception('%s does not exist, and a revision was not '
                            'specified' % tracking_file)
        # noinspection PyTypeChecker
        with open(tracking_file, 'rb') as f:
            starting_revision = f.read().strip()
        # As the tracking file stores the last processed revision, we need
        # work on its descendants, but not the revision itself.
        revset = 'descendants({rev}) - {rev}'.format(rev=starting_revision)

    configs = ['ui.interactive=False']
    with hglib.open(integration_repo_path, 'utf-8', configs) as hg_repo:
        # Update repo if required.
        local_tip = hg_repo.identify(id=True).strip()
        remote_tip = subprocess.check_output(
            [hglib.HGPATH, 'identify', '-r', 'tip', integration_repo_url]
        ).strip()
        if local_tip != remote_tip:
            logger.info('updating %s to %s' % (integration_repo_path,
                                               remote_tip))
            hg_repo.pull(source=integration_repo_url, rev=remote_tip)
            hg_repo.update(rev=remote_tip)

        # Grab list of backout commits and process.
        backout_commits = _find_backout_commits(hg_repo, revset)
        if backout_commits:
            github_pr = GitHubPR(github_token, github_repo_name,
                                 github_repo_path)

            # Transient failures here could result in the repos being out of
            # sync; retry failures before giving up.
            attempts_remaining = 5
            while True:
                try:
                    _create_pr_from_backout(
                        integration_repo_path, hg_repo,
                        github_pr, backout_commits,
                        pull_request_author)
                    break
                except Exception as e:
                    attempts_remaining -= 1
                    if attempts_remaining == 0:
                        raise
                    logger.error('failed to create pull-request, retrying: %s'
                                 % str(e))

    # Mark tip as processed.
    # noinspection PyTypeChecker
    with open(tracking_file, 'wb') as f:
        f.write(b'%s\n' % remote_tip)

    # TODO so hacky. Relies on credentials in the environment.
    if tracking_s3_upload_url:
        try:
            cmd = [b'aws', b's3', b'cp', tracking_file, tracking_s3_upload_url]
            logger.info('executing %s' % cmd)
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as e:
            logger.warn(e.output)
