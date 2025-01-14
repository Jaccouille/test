#!/usr/bin/python3.8

# Standard library imports:
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from configparser import ConfigParser
from functools import partial
from getpass import getpass
from itertools import takewhile
from operator import methodcaller
from os import chdir
from os.path import exists, join
from subprocess import run
from sys import exit

# Third-party imports:
from boltons.iterutils import first
from git import Repo
from github import Github
from jinja2 import Template
from parse import search
from yaml import dump, Dumper, load, Loader

# Local Imports:
from generate_loading_screen import generate_loading_screen

# The template for the changelog file.
template = Template(
    """
package {{ package }}

import ChangeLog

init
    new ChangeLog({{ major }}, {{ minor }}, '{{ patch }}')
{% for change in changelog %}
        ..add("{{change}}")
{% endfor %}
""".lstrip(),
    trim_blocks=True
)


def build_parser():
    # Create the base parser.
    parser = ArgumentParser(
        "Release",
        description="Updates and releases the map based on recent commits.",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )

    # Add the independent arguments.
    parser.add_argument("--last",    help="SHA of the last release.")
    parser.add_argument("--path",    help="Path of the project.")
    parser.add_argument("--base",    help="Path of the map.")
    parser.add_argument("--repo",    help="Name of the repository to update.")
    parser.add_argument("--owner",   help="Location of the target repository.")
    parser.add_argument("--remote",  help="Name of the corresponding remote.")
    parser.add_argument("--dry-run", help="Disable publishing changes.",
                        action="store_true")

    # Add the arguments for GitHub credentials.
    login = parser.add_mutually_exclusive_group(required=True)
    login.add_argument("--token",    help="PAT used for GitHub.")
    login.add_argument("--username", help="Username for GitHub.")

    # Create the argument group used to indicate the version upgrade.
    group = parser.add_mutually_exclusive_group(required=True)
    for part in ["major", "minor", "patch"]:
        group.add_argument(
            f"--{part}",
            action="store_true",
            help=f"Upgrade the {part} version."
        )

    # Add the default values.
    parser.set_defaults(**{
        "base":   "base.w3x",
        "path":   ".",
        "remote": "origin",
    })

    # Output the finished parser.
    return parser


# Gets the version of the map, based on the previous release.
def get_version(repo):
    return first(repo.get_releases()).title


# Gets the SHA for the commit for the given version, which is based on the tag.
def get_sha(repo, version):
    for tag in repo.get_tags():
        if tag.name == version:
            return tag.commit.sha


# Updates the build file with new version.
def update_build(version):
    # Compute the path of the configuration file.
    path = "wurst.build"

    # Read the build file.
    with open(path) as target:
        build = load(target, Loader=Loader)

    # Look up the previous name of the map.
    name = build["buildMapData"]["name"]

    # Construct the new name for the map.
    name = f"{name.rsplit(maxsplit=1)[0]} {version}"

    # Strip whitespace for the filename to ease access.
    # Remove color code if it exist in the name string
    filename = ".".join((name[10:] if name[0] == "|" else name).split())

    # Update the build file.
    build["buildMapData"].update({
        "name":     name,
        "fileName": filename,
    })

    # Write the build file.
    with open(path, "w") as target:
        dump(build, target, Dumper=Dumper, sort_keys=False)

    # Output the paths for the build file and built map.
    return path, join("_build", f"{filename}.w3x")


def get_changelog(repo, sha, marker="$changelog: "):
    # Accept abbreviations by allowing partial matches for the SHA.
    def predicate(commit):
        return not commit.sha.startswith(sha)

    # Iterate over the commit history until reaching the given SHA.
    for commit in takewhile(predicate, repo.get_commits()):
        # Examine each PR associated with the commit.
        for pull in commit.get_pulls():
            # Consider each line of the PR message separately.
            for line in pull.body.split("\n"):
                # Verify that the line marks a changelog item.
                if line.startswith(marker):
                    # Output the changelog message and the associated PR.
                    yield line.lstrip(marker).rstrip(), pull


def write_changelog(major, minor, patch, changelog):
    # Compute the package name.
    package = f"v{major}_{minor:03}_{patch}"

    # Capture the arguments as a dictionary.
    kwargs = locals()

    # Compute the path of the changelog file.
    path = join("wurst", "changelogs", f"{package}.wurst")

    # Construct the path of the file.
    with open(path, "w") as package:
        package.write(template.render(**kwargs))

    # Output the path for later use.
    return path


# Extracts the list of WurstScript arguments from the local arguments file.
def get_args():
    # Open the static location.
    with open("wurst_run.args") as args:
        # Filter for lines marking arguments.
        for line in filter(methodcaller("startswith", "-"), args.readlines()):
            # Strip the newline characters.
            yield line.rstrip()


# Build the output map file using `grill` from WurstScript.
def build_map(base, target):
    # Run the command used to build the map
    command = run(
        # Forward the list of arguments from the arguments file.
        ["grill", "build", base, *get_args()],
        # Capture the output to avoid excessive noise.
        capture_output=True
    )

    # Validate that the build succeeded, independent of the return code.
    if not exists(target):
        exit(f"Wurst build failed: {' '.join(command.args)}")


# Updates the remote using basic git, rather than the GitHub API.
def update_repo(remote, files, version):
    # Construct the repository, based on the working directory.
    repo = Repo()

    # Add all modified files to the index.
    repo.index.add(files)

    # Create the commit.
    commit = repo.index.commit(f"Updated configuration for {version} release.")

    # Push the commit.
    result = repo.remote(remote).push()[0]

    # Validate the result against the 1024 error bit.
    if result.flags & 1 << 10:
        # Undo the latest commit.
        repo.head.reset(commit.parents[0])

        # Notify the user of the failure
        exit(f"Push failure: {result.summary.strip()}")


# Constructs the GitHub repository object based on the local git project.
def get_repo(remote):
    # Fetch the URL for the given remote.
    url = Repo().remote(remote).url

    # List the patterns a GitHub repository URL can assume.
    patterns = [
        "git@github.com:{}/{}.git",
        "https://github.com/{}/{}.git",
    ]

    # Attempt to parse the URL.
    if not (match := first(map(partial(search, string=url), patterns))):
        exit(f"Invalid remote URL: {url}")

    # Construct and output the repository target.
    return "/".join(match)


if __name__ == "__main__":
    # Create the parser.
    parser = build_parser()

    # Parse the arguments.
    args = parser.parse_args()

    # Update the working directory.
    chdir(args.path)

    # Create the client for GitHub interaction.
    if args.token:
        github = Github(args.token)
    else:
        github = Github(args.username, getpass())

    # Look up the repository.
    repo = github.get_repo(get_repo(args.remote))

    # Fetch the previous version.
    version = get_version(repo)

    # Fetch the SHA associated with that version.
    sha = args.last or get_sha(repo, version)

    # Parse the version.
    major, minor, patch = search("v{:d}.{:d}{}", version)

    # Advance the specified version attribute.
    if args.major:
        major += 1
        minor = 0
        patch = "a"
    # Block the minor version from exceeding three digits.
    elif args.minor and args.minor != 999:
        minor += 1
        patch = "a"
    # Block the patch version from exceeding a lowercase letter.
    elif patch != "z":
        patch = chr(ord(patch) + 1)
    else:
        parser.error("Cannot increment version further.")

    # Update the version.
    version = f"v{major}.{minor}{patch}"

    # Compute the changelog.
    changes, pulls = zip(*sorted(get_changelog(repo, sha)))

    # Write the changelog package.
    package = write_changelog(major, minor, patch, changes)

    # Update the build file for the map.
    build, target = update_build(version)

    # Draw changelogs on the loading screen
    generate_loading_screen(changelogs=list(changes), version=version)

    # Construct the markdown used to link the changelog.
    changelog = "\n".join(
        f"[{change}]({pull.html_url})"
        for change, pull in zip(changes, pulls)
    )

    # Update the repository with the modified files.
    if args.dry_run:
        print("Changelog:", changelog, sep="\n")
    else:
        # Verify that the map can be built.
        build_map(args.base, target)

        # Push the changes.
        update_repo(args.remote, [package, build], version)

        # Release the changes.
        repo.create_git_release(
            tag=version,
            name=version,
            message=changelog,
            target_commitish=repo.get_branch("master"),
        ).upload_asset(target)
