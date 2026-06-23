"""
github_relay.py — GitHub integration for Relay.

One markdown file per topic is the permanent, shareable thread.
Any agent with GitHub access can read the raw URL and get full context.
Writes run in background threads so they never block the UI.
"""
import re
import threading
from typing import Optional


def topic_to_path(topic: str) -> str:
    """Turn a topic string into a stable file path."""
    slug = re.sub(r"[^a-z0-9\s-]", "", topic.lower())
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")[:80] or "conversation"
    return f"conversations/{slug}.md"


def get_repo(token: str, repo_name: str):
    """Connect to the repo, creating it if it doesn't exist yet."""
    from github import Github, GithubException
    g    = Github(token)
    user = g.get_user()
    try:
        return g.get_repo(f"{user.login}/{repo_name}")
    except GithubException:
        return user.create_repo(
            repo_name,
            description="Relay — shared AI conversation threads",
            private=False,
            auto_init=True,
        )


def _read(repo, path: str) -> tuple[Optional[str], Optional[str]]:
    try:
        f = repo.get_contents(path)
        return f.decoded_content.decode("utf-8"), f.sha
    except Exception:
        return None, None


def _write(repo, path: str, content: str, message: str) -> None:
    _, sha = _read(repo, path)
    if sha:
        repo.update_file(path, message, content, sha)
    else:
        repo.create_file(path, message, content)


def append_round(
    repo,
    topic: str,
    agent_names: list[str],
    user_msg: Optional[str],
    replies: dict[str, str],
) -> str:
    """
    Append one conversation round to the topic file and commit.
    Returns the GitHub HTML URL for the file.
    """
    path    = topic_to_path(topic)
    current, _ = _read(repo, path)

    if current is None:
        agent_str = "  ·  ".join(a.upper() for a in agent_names)
        current   = f"# {topic}\n\n**Agents:** {agent_str}\n\n---\n\n"

    block = ""
    if user_msg:
        block += f"## You\n{user_msg}\n\n---\n\n"
    for name, reply in replies.items():
        block += f"### {name.upper()}\n{reply}\n\n---\n\n"

    _write(repo, path, current + block, f"Relay: {topic[:60]}")
    return html_url(repo, topic)


def html_url(repo, topic: str) -> str:
    return f"https://github.com/{repo.full_name}/blob/main/{topic_to_path(topic)}"


def raw_url(repo, topic: str) -> str:
    return f"https://raw.githubusercontent.com/{repo.full_name}/main/{topic_to_path(topic)}"


def commit_async(fn, *args, **kwargs) -> None:
    """Fire-and-forget GitHub write — never blocks the UI."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()
