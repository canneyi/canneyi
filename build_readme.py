from python_graphql_client import GraphqlClient
import httpx
import json
import pathlib
import re
import os
import sys

root = pathlib.Path(__file__).parent.resolve()
client = GraphqlClient(endpoint="https://api.github.com/graphql")

TOKEN = os.environ.get("GH_TOKEN", "")
GITHUB_USER = "canneyi"

SKIP_REPOS: set[str] = set()


def replace_chunk(content, marker, chunk, inline=False):
    r = re.compile(
        r"<!\-\- {} starts \-\->.*<!\-\- {} ends \-\->".format(marker, marker),
        re.DOTALL,
    )
    if not inline:
        chunk = "\n{}\n".format(chunk)
    chunk = "<!-- {} starts -->{}<!-- {} ends -->".format(marker, chunk, marker)
    return r.sub(chunk, content)


GRAPHQL_SEARCH_QUERY = """
query {
  search(first: 100, type:REPOSITORY, query:"is:public user:%s sort:updated") {
    nodes {
      __typename
      ... on Repository {
        name
        description
        url
        releases(orderBy: {field: CREATED_AT, direction: DESC}, first: 1) {
          totalCount
          nodes {
            name
            publishedAt
            url
          }
        }
      }
    }
  }
}
""" % GITHUB_USER

RELEASES_CACHE_PATH = root / "releases_cache.json"

GRAPHQL_SEARCH_QUERY_PAGINATED = """
query {
  search(first: 100, type:REPOSITORY, query:"is:public user:%s sort:updated", after: AFTER) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      __typename
      ... on Repository {
        name
        description
        url
        releases(orderBy: {field: CREATED_AT, direction: DESC}, first: 1) {
          totalCount
          nodes {
            name
            publishedAt
            url
          }
        }
      }
    }
  }
}
""" % GITHUB_USER


def load_releases_cache():
    """Load cached releases from JSON file, keyed by repo name."""
    if RELEASES_CACHE_PATH.exists():
        return json.loads(RELEASES_CACHE_PATH.read_text())
    return {}


def save_releases_cache(cache):
    """Save releases cache to JSON file."""
    RELEASES_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def seed_releases_cache(oauth_token):
    """Fetch ALL releases via pagination to seed the cache. Run once with --seed."""
    cache = {}
    has_next_page = True
    after_cursor = None

    while has_next_page:
        query = GRAPHQL_SEARCH_QUERY_PAGINATED.replace(
            "AFTER", '"{}"'.format(after_cursor) if after_cursor else "null"
        )
        data = client.execute(
            query=query,
            headers={"Authorization": "Bearer {}".format(oauth_token)},
        )
        print(json.dumps(data, indent=4))

        repo_nodes = data["data"]["search"]["nodes"]
        for repo in repo_nodes:
            if repo["name"] in SKIP_REPOS:
                continue
            if repo["releases"]["totalCount"]:
                cache[repo["name"]] = {
                    "repo": repo["name"],
                    "repo_url": repo["url"],
                    "description": repo["description"],
                    "release": repo["releases"]["nodes"][0]["name"]
                    .replace(repo["name"], "")
                    .strip(),
                    "published_at": repo["releases"]["nodes"][0]["publishedAt"],
                    "published_day": repo["releases"]["nodes"][0]["publishedAt"].split("T")[0],
                    "url": repo["releases"]["nodes"][0]["url"],
                    "total_releases": repo["releases"]["totalCount"],
                }

        after_cursor = data["data"]["search"]["pageInfo"]["endCursor"]
        has_next_page = after_cursor

    save_releases_cache(cache)
    print(f"Seeded cache with {len(cache)} releases")
    return list(cache.values())


def fetch_and_update_releases(oauth_token):
    """Fetch recent releases and merge into cache. Returns list of all releases."""
    # Load existing cache
    cache = load_releases_cache()

    # Fetch the 100 most recently updated repos (single API call)
    data = client.execute(
        query=GRAPHQL_SEARCH_QUERY,
        headers={"Authorization": "Bearer {}".format(oauth_token)},
    )
    print()
    print(json.dumps(data, indent=4))
    print()

    # Update cache with fresh data
    repo_nodes = data["data"]["search"]["nodes"]
    for repo in repo_nodes:
        if repo["name"] in SKIP_REPOS:
            continue
        if repo["releases"]["totalCount"]:
            cache[repo["name"]] = {
                "repo": repo["name"],
                "repo_url": repo["url"],
                "description": repo["description"],
                "release": repo["releases"]["nodes"][0]["name"]
                .replace(repo["name"], "")
                .strip(),
                "published_at": repo["releases"]["nodes"][0]["publishedAt"],
                "published_day": repo["releases"]["nodes"][0]["publishedAt"].split("T")[0],
                "url": repo["releases"]["nodes"][0]["url"],
                "total_releases": repo["releases"]["totalCount"],
            }

    # Save updated cache
    save_releases_cache(cache)

    # Return as list
    return list(cache.values())


if __name__ == "__main__":
    readme = root / "README.md"
    project_releases = root / "releases.md"

    if "--seed" in sys.argv or not RELEASES_CACHE_PATH.exists():
        print("Seeding releases cache with full pagination...")
        releases = seed_releases_cache(TOKEN)
    else:
        releases = fetch_and_update_releases(TOKEN)
    releases.sort(key=lambda r: r["published_at"], reverse=True)
    md = "\n\n".join(
        [
            "[{repo} {release}]({url}) - {published_day}".format(**release)
            for release in releases[:8]
        ]
    ) or "_No releases yet._"
    readme_contents = readme.open().read()
    rewritten = replace_chunk(readme_contents, "recent_releases", md)

    # Write out full project-releases.md file
    project_releases_md = "\n".join(
        [
            (
                "* **[{repo}]({repo_url})**: [{release}]({url}) {total_releases_md}- {published_day}\n"
                "<br />{description}"
            ).format(
                total_releases_md="- ([{} releases total]({}/releases)) ".format(
                    release["total_releases"], release["repo_url"]
                )
                if release["total_releases"] > 1
                else "",
                **release
            )
            for release in releases
        ]
    ) or "_No releases yet._"
    if project_releases.exists():
        project_releases_content = project_releases.open().read()
        project_releases_content = replace_chunk(
            project_releases_content, "recent_releases", project_releases_md
        )
        project_releases_content = replace_chunk(
            project_releases_content, "project_count", f"{len(releases):,}", inline=True
        )
        project_releases_content = replace_chunk(
            project_releases_content,
            "releases_count",
            "{:,}".format(sum(r["total_releases"] for r in releases)),
            inline=True,
        )
        project_releases.open("w").write(project_releases_content)

    readme.open("w").write(rewritten)
