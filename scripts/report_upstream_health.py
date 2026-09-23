#!/usr/bin/env python3
"""Keep one actionable update issue without hiding a failed maintenance run."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

MARKER = "<!-- computer-use-upstream-health -->"
TITLE = "Engine upstream update blocked"


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def reconcile(failed, repository, run_url, results, command=gh):
    pages = json.loads(command(
        "api", "--paginate", "--slurp",
        f"repos/{repository}/issues?state=all&per_page=100",
    ))
    matches = [issue for page in pages for issue in page
               if "pull_request" not in issue and MARKER in (issue.get("body") or "")]
    # Prefer an open issue, then reuse the oldest historical issue after recovery.
    matches.sort(key=lambda issue: (issue["state"] != "open", issue["number"]))
    existing = matches[0] if matches else None
    if failed:
        details = "\n".join(f"- `{name}`: **{result['result']}**"
                            for name, result in sorted(results.items()))
        body = f"""{MARKER}
The weekly engine update could not complete. The workflow remains failed until a later healthy run.

[Inspect the failed run and job logs]({run_url}). The preparation job uploads its merge log and candidate patch as `engine-update-diagnostics`, including paths that need manual conflict resolution.

{details}

To recover:
1. For merge conflicts, run `python3 computer-use-linux/scripts/sync_upstream.py prepare` on a clean checkout and reconcile the reported upstream paths with the local patches. Review `computer-use-linux/UPSTREAM.toml` before committing a new pin.
2. For verification failures, reproduce the failing CI, stock Codex, or Hyprland job against the candidate commit recorded in the preparation log. Fix the regression before updating the pin.
3. For publication or permissions failures, inspect the publishing job and enable Actions to create pull requests if necessary. Then rerun `computer-use-upstream-sync` from Actions.

No update is merged automatically. This issue is updated in place on repeated failures and closed by the next successful run.
"""
        with tempfile.TemporaryDirectory(prefix="upstream-health-") as directory:
            body_path = Path(directory) / "body.md"
            body_path.write_text(body)
            if existing:
                if existing["state"] == "closed":
                    command("issue", "reopen", str(existing["number"]), "--repo", repository)
                command("issue", "edit", str(existing["number"]), "--repo", repository,
                        "--title", TITLE, "--body-file", str(body_path))
            else:
                command("issue", "create", "--repo", repository,
                        "--title", TITLE, "--body-file", str(body_path))
    else:
        for issue in matches:
            if issue["state"] == "open":
                command("issue", "close", str(issue["number"]), "--repo", repository,
                        "--comment", f"The upstream update check recovered: {run_url}")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failed", choices=["true", "false"], required=True)
    args = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    run_url = (f"{os.environ['GITHUB_SERVER_URL']}/{repository}/actions/runs/"
               f"{os.environ['GITHUB_RUN_ID']}")
    return reconcile(args.failed == "true", repository, run_url,
                     json.loads(os.environ["UPDATE_JOB_RESULTS"]))


if __name__ == "__main__":
    raise SystemExit(main())
