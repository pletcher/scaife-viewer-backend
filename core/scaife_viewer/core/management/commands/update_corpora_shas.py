"""
Backported from https://github.com/scaife-viewer/scaife-cts-api/blob/0e3d702dcdeae966f8d37935e7c657e16c039cf3/scaife_cts_api/update_corpus_shas.py
"""
import os

from django.core.management.base import BaseCommand

import ruamel.yaml as yaml
from github import Github, UnknownObjectException

from ...hooks import hookset


class ReleaseResolver:
    def __init__(self, client, repo_name, data):
        self.client = client
        self.repo_name = repo_name
        self.sha = data["sha"]

    def fetch_latest_release(self, repo):
        latest_release = repo.get_latest_release()
        self.ref = latest_release.tag_name
        # NOTE: latest_commit_sha will differ from latest_release.target_committish, because
        # the release was created and then the tag was advanced if a HookSet was used
        self.latest_commit_sha = repo.get_commit(latest_release.target_commitish).sha
        # self.latest_commit_sha = repo.get_commit(self.ref).sha
        self.tarball_url = latest_release.tarball_url

    def fetch_latest_commit(self, repo):
        default_branch = repo.get_branch(repo.default_branch)
        self.ref = default_branch.name
        self.latest_commit_sha = default_branch.commit.sha
        self.tarball_url = f"https://api.github.com/repos/{self.repo_name}/tarball/{self.latest_commit_sha}"

    def resolve_release(self):
        diff_url = ""
        self.repo = self.client.get_repo(self.repo_name)
        # prefer default branch within the "manifest" approach
        try:
            self.fetch_latest_release(self.repo)
        except UnknownObjectException:
            print(
                f'{self.repo_name} has no release data.  retrieving latest SHA from "{self.repo.default_branch}"'
            )
            self.fetch_latest_commit(self.repo)

        should_update = self.latest_commit_sha != self.sha
        if should_update:
            compared = self.repo.compare(self.sha, self.latest_commit_sha)
            diff_url = compared.html_url
        return should_update, diff_url

    def emit_status(self, should_update, diff_url):
        return [
            self.repo.full_name,
            should_update,
            self.sha,
            self.latest_commit_sha,
            diff_url,
        ]

    def update_corpus(self, corpus_dict):
        should_update, diff_url = self.resolve_release()

        corpus_dict[self.repo_name] = dict(
            ref=self.ref,
            sha=self.latest_commit_sha,
            tarball_url=self.tarball_url,
        )
        return self.emit_status(should_update, diff_url)


class Command(BaseCommand):
    def handle(self, *args, **options):
        """
        Small helper script used to update to latest releases
        of corpus repos.

        If releases are not found, defaults to the last commit
        on `master`.
        """
        ACCESS_TOKEN = os.environ.get("GITHUB_ACCESS_TOKEN", "")
        if ACCESS_TOKEN:
            client = Github(ACCESS_TOKEN)
        else:
            client = Github()

        statuses = []
        manifest = hookset.content_manifest_path
        corpus = yaml.round_trip_load(manifest.open())
        new_corpus = dict()
        for repo_name, data in corpus.items():
            resolver = ReleaseResolver(client, repo_name, data)

            status_result = resolver.update_corpus(new_corpus)
            statuses.append(status_result)

        yaml.round_trip_dump(new_corpus, manifest.open("w"))
